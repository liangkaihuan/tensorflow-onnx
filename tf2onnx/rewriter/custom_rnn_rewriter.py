# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.rewriter.custom_rnn_rewriter - custom rnn support
"""

from __future__ import division
from __future__ import print_function
import logging
import sys
import traceback
from onnx import helper, onnx_pb
import numpy as np
from tf2onnx.graph import Graph, Node
from tf2onnx.graph_matcher import OpTypePattern, GraphMatcher
from tf2onnx.rewriter.loop_rewriter_base import LoopRewriterBase, Context
from tf2onnx.rewriter.rnn_utils import is_tensor_array_gather_op, is_tensor_array_write_op, \
     make_onnx_node
from tf2onnx.rewriter.rnn_utils import BodyGraphDict, REWRITER_RESULT, SubGraphMetadata
from tf2onnx.tfonnx import utils


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tf2onnx.rewriter.custom_rnn_rewriter")
INVLAID_INPUT_ID = "invalid:0"

# pylint: disable=missing-docstring,invalid-name,unused-argument,using-constant-test,broad-except,protected-access


class CustomRnnContext(Context):
    def __init__(self):
        super(CustomRnnContext, self).__init__()
        self.other_loop_vars = {}
        self.rnn_scope = None

        self.output_tas = []
        self.input_tas = []
        self.time_var = None
        self.iteration_var = None


class TensorArrayProp(object):
    def __init__(self):
        self.index_input_id = None
        self.data_input_id = None
        self.output_id = None


class ScanProperties(object):
    def __init__(self, initial_state_and_scan_inputs, loop_state_inputs,
                 loop_state_outputs, loop_scan_inputs, loop_scan_outputs):
        self.initial_state_and_scan_inputs = initial_state_and_scan_inputs
        self.loop_state_inputs = loop_state_inputs
        self.loop_state_outputs = loop_state_outputs
        self.loop_scan_inputs = loop_scan_inputs
        self.loop_scan_outputs = loop_scan_outputs


class CustomRnnRewriter(LoopRewriterBase):
    def __init__(self, g):
        super(CustomRnnRewriter, self).__init__(g)
        self.rnn_input_pattern = \
            OpTypePattern('TensorArrayReadV3', name='ta_read', inputs=[
                OpTypePattern("Enter", name="ta_enter", inputs=[
                    OpTypePattern("TensorArrayV3")
                ]),
                OpTypePattern('*'),
                OpTypePattern("Enter", name="ta_scatter_enter", inputs=[
                    OpTypePattern("TensorArrayScatterV3", name="ta_input_scatter")
                ]),
            ])

    def create_context(self):
        return CustomRnnContext()

    def run(self):
        log.debug("enter custom rnn rewriter")
        return self.run_internal()

    def _get_rnn_scope_name(self, while_scope_name):
        parts = while_scope_name.split('/')
        rnn_scope = '/'.join(parts[0:-2]) + "/"
        log.debug("found rnn scope %s", rnn_scope)
        return rnn_scope

    def _parse_rnn_loop(self, context):
        # check a while loop is generated by dynamic_rnn or bidirectional_rnn by
        #
        # 1. some patterns in _time_step in dynamic_rnn: tensor array read, tensor array write
        # 2. some patterns in control_flow_ops.while_loop in dynamic_rnn:
        #      cond: time < loop_bound
        #      loop_vars: (time, output_ta, state)
        #      time has name called "time"
        #      iteration_cnt is added by control flow.

        # be noted:
        # 1. iteration counter does not exist in tf1.4 or earlier versions
        # 2. if dynamic_rnn's first input is not consumed, output ta does not exist.
        time_name = context.rnn_scope + "time"
        ta_array_name_prefix = context.rnn_scope + "dynamic_rnn/output_"
        iteration_counter_name = context.while_context_scope + "iteration_counter"

        found_time = False
        is_rnn_out_ta = True
        for enter_name, val in context.loop_variables.items():
            enter_input_node = self.g.get_node_by_name(val.enter_input_id)
            if val.is_tensor_array:
                ta_name = enter_input_node.get_attr("tensor_array_name").s.decode("utf-8")
                if not ta_name.startswith(ta_array_name_prefix):
                    is_rnn_out_ta = False
            elif enter_input_node.name == time_name:
                found_time = True
                context.time_var = val
            elif enter_input_node.name == iteration_counter_name:
                context.iteration_var = val
            else:
                context.other_loop_vars[enter_name] = val

        if not (found_time and is_rnn_out_ta):
            log.debug("this should not be a dynamic_rnn loop, found_time: %s, is_rnn_out_ta: %s",
                      found_time, is_rnn_out_ta)
            return False

        return True

    def need_rewrite(self, context):
        context.rnn_scope = self._get_rnn_scope_name(context.while_context_scope)

        if not self._parse_rnn_loop(context):
            log.debug("skip the loop due to parse_rnn_loop failed")
            return False

        self._parse_time_var(context)
        self._parse_output_ta(context)
        self._parse_input_ta(context)

        if not (context.input_tas or context.output_tas):
            log.debug("this should not be a dynamic_rnn loop, no ta input or output are found")
            return False
        return True

    def rewrite(self, context):
        log.debug("enter rewrite function")
        scan_node = None
        try:
            to_remove = self._cut_off_connection_for_cell(context)
            all_nodes = self.g.get_nodes()
            for n in set(to_remove):
                if n in all_nodes:
                    all_nodes.remove(n)
            self.g.set_nodes(all_nodes)

            scan_props, nodes_to_append = self._compose_cell_inputs_and_outputs(context)
            scan_node = self._create_scan_node(context, scan_props)
            if not scan_node:
                log.error("failed to create scan node during rewrite")
                return REWRITER_RESULT.FAIL
            nodes_to_append.append(scan_node)

            _ = self._extract_and_register_cell_graph_info(context, scan_props, scan_node)

            to_append = self._connect_scan_with_output(context, scan_node)
            nodes_to_append.extend(to_append)
            all_nodes = self.g.get_nodes()
            all_nodes.extend(nodes_to_append)
            self.g.set_nodes(all_nodes)

            return REWRITER_RESULT.OK
        except Exception as ex:
            tb = traceback.format_exc()
            if scan_node and BodyGraphDict.has_body_graph_info(scan_node.name):
                BodyGraphDict.pop_body_graph_info(scan_node.name)
                log.error("remove scan node body graph from dict")
            log.error("rewrite failed, due to exception: %s, details:%s", ex, tb)
            return REWRITER_RESULT.FAIL

    def _parse_time_var(self, context):
        time_var = context.time_var
        log.debug("time var %s - enter input id (%s) shape: %s, output (%s) shape: %s", time_var.enter_name,
                  time_var.enter_input_id, self.g.get_shape(time_var.enter_input_id),
                  time_var.switch_true_identity_output_id, self.g.get_shape(time_var.switch_true_identity_output_id))

    def _parse_output_ta(self, context):
        for enter_name, loop_var in context.loop_variables.items():
            if not loop_var.is_tensor_array:
                continue

            output_ta = TensorArrayProp()
            output_ta.data_input_id = loop_var.next_iteration_input_id

            output_ta.index_input_id = loop_var.ta_index_id
            if loop_var.exit_output_id:
                exit_consumers = self.g.find_output_consumers(loop_var.exit_output_id)
                ta_gather_node = [n for n in exit_consumers if is_tensor_array_gather_op(n)][0]
                output_ta.output_id = ta_gather_node.output[0]

            context.output_tas.append(output_ta)
            log.debug("output ta %s - data input (%s) shape: %s, output (%s) shape: %s", enter_name,
                      output_ta.data_input_id, self.g.get_shape(output_ta.data_input_id),
                      output_ta.output_id, self.g.get_shape(output_ta.output_id))

    def _parse_input_ta(self, context):
        matcher = GraphMatcher(self.rnn_input_pattern, allow_reorder=True)
        match_results = list(matcher.match_ops(self.g.get_nodes()))
        match_results = [r for r in match_results if r.get_op("ta_input_scatter").name.startswith(context.rnn_scope)]
        for match in match_results:
            ta_input_scatter = match.get_op("ta_input_scatter")
            # the 3rd input of scatter is the value
            input_ta = TensorArrayProp()

            # dynamic_rnn specific approach.
            input_ta.data_input_id = ta_input_scatter.input[2]

            ta_read_node = match.get_op("ta_read")
            input_ta.index_input_id = ta_read_node.input[1]
            input_ta.output_id = match.get_op("ta_read").output[0]

            input_shape = self.g.get_shape(input_ta.data_input_id)
            output_shape = self.g.get_shape(input_ta.output_id)
            if output_shape is None and input_shape is not None:
                self.g.set_shape(input_ta.output_id, input_shape[1:])

            context.input_tas.append(input_ta)

            log.debug("input ta %s - data input (%s) shape: %s, output (%s) shape: %s", ta_read_node.name,
                      input_ta.data_input_id, self.g.get_shape(input_ta.data_input_id),
                      input_ta.output_id, self.g.get_shape(input_ta.output_id))

    def _cut_off_connection_for_cell(self, context):
        nodes_to_remove = []
        all_vars = [context.time_var]
        all_vars += [val for _, val in context.other_loop_vars.items()]
        for val in all_vars:
            # remove the node to cut off a starting node of the cell (e.g. loop body).
            nodes_to_remove.append(self.g.get_node_by_name(val.switch_true_identity_output_id))

            # connect NextIteration to an invalid node, to cut off a ending node of the cell.
            next_iter_nodes = [n for n in self.g.get_nodes() if n.type == "NextIteration"]
            self.g.replace_all_inputs(next_iter_nodes, val.next_iteration_input_id, INVLAID_INPUT_ID)

        for input_ta in context.input_tas:
            # remove the node to cut off connection between scan_input and the cell.
            nodes_to_remove.append(self.g.get_node_by_name(input_ta.output_id))

        for output_ta in context.output_tas:
            # remove the node to cut off connection between scan_output and the cell.
            ta_write_nodes = [n for n in self.g.get_nodes() if is_tensor_array_write_op(n)]
            self.g.replace_all_inputs(ta_write_nodes, output_ta.data_input_id, INVLAID_INPUT_ID)

        return nodes_to_remove

    def _compose_cell_inputs_and_outputs(self, context):
        log.debug("_compose_cell_inputs_and_outputs")

        nodes_to_append = []
        loop_state_inputs = []
        loop_state_outputs = []
        initial_state_and_scan_inputs = []

        # change time shape to {1} since current Scan does not support
        time_var, to_append = self._adapt_time_var_as_workaround(context.time_var)
        nodes_to_append.extend(to_append)

        log.debug("prepare cell state inputs")
        vars_to_iterate = [time_var] + [val for _, val in context.other_loop_vars.items()]
        for var in vars_to_iterate:
            nodes = self._adapt_scan_sequence_input_or_output("input", var.enter_input_id, False)
            var.enter_input_id = nodes[-1].output[0]
            nodes_to_append.extend(nodes)

            loop_state_inputs.append(var.switch_true_identity_output_id)
            loop_state_outputs.append(var.next_iteration_input_id)
            initial_state_and_scan_inputs.append(var.enter_input_id)

        log.debug("prepare cell scan inputs")
        loop_scan_inputs = []
        for input_ta in context.input_tas:
            nodes = self._adapt_scan_sequence_input_or_output("input_ta", input_ta.data_input_id, False)
            input_ta.data_input_id = nodes[-1].output[0]
            nodes_to_append.extend(nodes)

            loop_scan_inputs.append(input_ta.output_id)
            initial_state_and_scan_inputs.append(input_ta.data_input_id)

        log.debug("prepare cell scan outputs")
        loop_scan_outputs = []
        for output_ta in context.output_tas:
            loop_scan_outputs.append(output_ta.data_input_id)

        scan_props = ScanProperties(initial_state_and_scan_inputs, loop_state_inputs, loop_state_outputs,
                                    loop_scan_inputs, loop_scan_outputs)

        return scan_props, nodes_to_append

    def _create_scan_node(self, context, scan_props):
        log.debug("create scan node")
        # here we did not give the sequence_length, because
        # current batch size is 1, not original batch size
        # original seq_length will be used by the loop body of Scan op.
        scan_node = make_onnx_node(self.g, "Scan", [""] + scan_props.initial_state_and_scan_inputs,
                                   attr={"num_scan_inputs": len(scan_props.loop_scan_inputs)},
                                   output_count=len(scan_props.loop_state_outputs + scan_props.loop_scan_outputs),
                                   skip_conversion=True)

        # the first state var is time-iterator.
        index = 0
        time_input_shape = self.g.get_shape(scan_node.input[1])
        time_input_dtype = self.g.get_dtype(scan_node.input[1])

        log.debug("_create_scan_node - set scan state_output shape for %s[%s]:%s",
                  scan_node.name, index, time_input_shape)
        self.g.set_shape(scan_node.output[index], time_input_shape)
        self.g.set_dtype(scan_node.output[index], time_input_dtype)
        index += 1

        # for other state vars
        state_input_shape = self.g.get_shape(scan_node.input[2])
        state_input_dtype = self.g.get_dtype(scan_node.input[2])
        for i in range(len(scan_props.loop_state_outputs) - 1):
            log.debug("_create_scan_node - set scan state_output shape for %s[%s]:%s",
                      scan_node.name, index, state_input_shape)
            self.g.set_shape(scan_node.output[index], state_input_shape)
            self.g.set_dtype(scan_node.output[index], state_input_dtype)
            index += 1

        last_scan_input_shape = self.g.get_shape(scan_node.input[-1])
        batch = last_scan_input_shape[0] # should be 1
        time = last_scan_input_shape[1]
        for i in range(len(scan_props.loop_scan_outputs)):
            scan_out_dtype = self.g.get_dtype(scan_props.loop_scan_outputs[i])
            output_shape = self.g.get_shape(scan_props.loop_scan_outputs[i])
            scan_output_shape = [batch, time] + output_shape
            log.debug("scan output [%s] has shape %s, batch:%s, time: %s, cell output shape: %s",
                      scan_props.loop_scan_outputs[i], scan_output_shape, batch, time, output_shape)
            log.debug("_create_scan_node - set scan scan_output shape for %s[%s]:%s",
                      scan_node.name, index, scan_output_shape)
            self.g.set_shape(scan_node.output[index], scan_output_shape)
            self.g.set_dtype(scan_node.output[index], scan_out_dtype)
            index += 1

        return scan_node

    def _extract_and_register_cell_graph_info(self, context, scan_props, scan_node):
        log.debug("_extract_cell_graph_nodes")

        sub_graph_inputs = scan_props.loop_state_inputs + scan_props.loop_scan_inputs
        sub_graph_outputs = scan_props.loop_state_outputs + scan_props.loop_scan_outputs
        body_graph_meta = SubGraphMetadata(self.g, sub_graph_inputs, sub_graph_outputs,
                                           scan_props.initial_state_and_scan_inputs)

        # according to input and output, find the body graph
        nodes, enter_nodes = self.find_subgraph(body_graph_meta, self.g)
        other_enter_input_ids = []
        for enter_node in enter_nodes:
            # connect Enter's output to Enter's input
            self.g.replace_all_inputs(self.g.get_nodes(), enter_node.output[0], enter_node.input[0])

            nodes = self.g._extract_sub_graph_nodes(self.g.get_node_by_name(enter_node.input[0]))

            other_enter_input_ids.append(enter_node.input[0])

        body_graph_meta.other_enter_input_ids = other_enter_input_ids

        log.debug("add body graph meta data into store")
        BodyGraphDict.add_body_graph_info(scan_node.name, body_graph_meta)
        return nodes

    def _connect_scan_with_output(self, context, scan_node):
        log.debug("connect scan output with the graph")

        index = 1 # ignore the 1st input (time-iterator)
        nodes_to_append = []
        for _, val in context.other_loop_vars.items():
            var_output_id = val.exit_output_id
            if var_output_id:
                nodes = self._adapt_scan_sequence_input_or_output("state_output_reshape",
                                                                  scan_node.output[index], True)
                nodes_to_append.extend(nodes)
                self.g.replace_all_inputs(self.g.get_nodes(), var_output_id, nodes[-1].output[0])

            index += 1

        for output_ta in context.output_tas:
            ta_final_output_id = output_ta.output_id
            if ta_final_output_id:
                nodes = self._adapt_scan_sequence_input_or_output("scan_output_reshape",
                                                                  scan_node.output[index], True)
                nodes_to_append.extend(nodes)
                self.g.replace_all_inputs(self.g.get_nodes(), ta_final_output_id, nodes[-1].output[0])
            index += 1

        return nodes_to_append

    def _adapt_scan_sequence_input_or_output(self, target_name, input_id, handle_output=False):
        nodes_to_add = []
        shape_node = make_onnx_node(self.g, "Shape", [input_id])
        nodes_to_add.append(shape_node)
        inferred_shape = self.g.get_shape(input_id)
        if handle_output is True:
            # handle output:
            # if required dim values don't contain more than one -1,
            # just use a const for Reshape's shape input.
            if inferred_shape is not None and inferred_shape[1:].count(-1) <= 1:
                new_shape_node = self.g.make_const(utils.make_name(target_name + "_target_shape"),
                                                   np.array(inferred_shape[1:], dtype=np.int64))
            else:
                # otherwise, get the dim dynamically, e.g. remove the fake batch size (e.g.1)
                # from [1, time, real-batch, ...]
                origin_shape_node = make_onnx_node(self.g, "Cast", [shape_node.output[0]],
                                                   {"to": onnx_pb.TensorProto.FLOAT})
                nodes_to_add.append(origin_shape_node)

                sliced_shape_node = make_onnx_node(self.g, "Slice", [origin_shape_node.output[0]],
                                                   {"axes": [0], "starts": [1], "ends": [sys.maxsize]})
                nodes_to_add.append(sliced_shape_node)

                new_shape_node = make_onnx_node(self.g, "Cast", [sliced_shape_node.output[0]],
                                                {"to": onnx_pb.TensorProto.INT64})
                nodes_to_add.append(new_shape_node)

            new_shape = inferred_shape[1:]
        else:
            # handle input:
            if inferred_shape is not None and inferred_shape.count(-1) <= 1:
                new_shape_node = self.g.make_const(utils.make_name(target_name + "_target_shape"),
                                                   np.array([1] + inferred_shape, dtype=np.int64))
            else:
                # add a fake batch size : 1
                fake_batch_size_node = self.g.make_const(utils.make_name(target_name + "_target_shape"),
                                                         np.array([1,], dtype=np.int64))
                new_shape_node = make_onnx_node(self.g, "Concat",
                                                [fake_batch_size_node.output[0], shape_node.output[0]],
                                                {"axis": 0})
                nodes_to_add.append(new_shape_node)
            new_shape = [1] + inferred_shape

        reshape_node = make_onnx_node(self.g, "Reshape", [input_id, new_shape_node.output[0]],
                                      skip_conversion=True, op_name_scope=target_name)
        nodes_to_add.append(reshape_node)
        self.g.set_shape(reshape_node.output[0], new_shape)
        self.g.set_dtype(reshape_node.output[0], self.g.get_dtype(input_id))
        log.debug("create Reshape for scan output %s, with output shape %s",
                  reshape_node.output[0], new_shape)
        return nodes_to_add

    # in theory, time var can be a scalar, but in current implementation of runtime, it could not be handled
    # correctly, so we unsqueeze it to a list containing a single element.
    def _adapt_time_var_as_workaround(self, var):
        log.debug("_adapt_time_var_as_workaround")
        nodes_to_append = []
        # change time shape to {1} since current Scan does not support
        time_init_node = self._create_unsqueeze_node("time_var_init", var.enter_input_id)
        nodes_to_append.append(time_init_node)
        var.enter_input_id = time_init_node.output[0]

        time_output_node = self._create_unsqueeze_node("time_var_output", var.next_iteration_input_id)
        nodes_to_append.append(time_output_node)
        var.next_iteration_input_id = time_output_node.output[0]

        time_input_node = self._create_squeeze_node("time_var_input", var.switch_true_identity_output_id)
        nodes_to_append.append(time_input_node)
        self.g.replace_all_inputs(self.g.get_nodes(), var.switch_true_identity_output_id, time_input_node.output[0])
        self.g.set_shape(var.switch_true_identity_output_id, [1] + self.g.get_shape(var.switch_true_identity_output_id))

        return var, nodes_to_append

    def _create_unsqueeze_node(self, target_name, input_id):
        unsqueeze_node = make_onnx_node(self.g, "Unsqueeze", [input_id], attr={"axes": [0]},
                                        skip_conversion=True, op_name_scope=target_name)
        input_shape = self.g.get_shape(input_id)
        if input_shape is None:
            raise ValueError(input_id + " is none")
        input_shape = [1] + input_shape
        self.g.set_shape(unsqueeze_node.output[0], input_shape)
        self.g.set_dtype(unsqueeze_node.output[0], self.g.get_dtype(input_id))

        return unsqueeze_node

    def _create_squeeze_node(self, target_name, input_id):
        squeeze_node = make_onnx_node(self.g, "Squeeze", [input_id], attr={"axes": [0]},
                                      skip_conversion=True, op_name_scope=target_name)
        input_shape = self.g.get_shape(input_id)
        if input_shape is None:
            raise ValueError(input_id + " is none")
        input_shape = list(input_shape)[1:]
        self.g.set_shape(squeeze_node.output[0], input_shape)
        self.g.set_dtype(squeeze_node.output[0], self.g.get_dtype(input_id))

        return squeeze_node
    # end of time var workaround


class CustomRnnLateRewriter(object):
    def __init__(self, g):
        self.g = g

    def rewrite(self):
        log.debug("enter custom rnn late rewriter")
        nodes = self.g.get_nodes()
        nodes_to_remove = []
        for scan_node in nodes:
            if scan_node.type != "Scan":
                continue
            log.debug("late write for scan node %s", scan_node.name)
            num_scan_inputs = scan_node.get_attr("num_scan_inputs").i
            if not BodyGraphDict.has_body_graph_info(scan_node.name):
                continue

            body_graph_meta = BodyGraphDict.pop_body_graph_info(scan_node.name)
            onnx_nodes, _ = LoopRewriterBase.find_subgraph(body_graph_meta, self.g)
            nodes_to_remove.extend(onnx_nodes)

            log.debug("start creating body graph for scan node %s ", scan_node.name)
            body_graph_initializers = {}
            const_nodes = [n for n in onnx_nodes if n.type in ("Const", "ConstV2")]
            for n in const_nodes:
                # when set nodes, Const should be removed, they need be replaced as initializers.
                body_graph_initializers[n.output[0]] = self.g.initializers[n.output[0]]
                onnx_nodes.remove(n)

            onnx_nodes = set(onnx_nodes)

            ops = []
            for op in onnx_nodes:
                onnx_op = op.op
                ops.append(onnx_op)

            body_g = Graph(ops, output_shapes=self.g._output_shapes, dtypes=self.g._dtypes)
            body_g._initializers = body_graph_initializers

            log.debug("start preparing body graph inputs nodes")
            temp_nodes = body_g.get_nodes()
            i = 0
            input_count = len(body_graph_meta.input_ids)
            for input_name, init_input_id in zip(body_graph_meta.input_ids, body_graph_meta.initial_input_ids):
                shape = body_g.get_shape(input_name)
                dtype = body_g.get_dtype(input_name)
                if shape is None:
                    shape = self.g.get_shape(init_input_id)
                    if i >= input_count - num_scan_inputs:
                        loop_input_shape = list(shape)[2:]  # delete [1, time,]
                    else:
                        loop_input_shape = list(shape)
                else:
                    loop_input_shape = list(shape)

                onnx_input_shape = utils.make_onnx_shape(loop_input_shape)
                val = helper.make_tensor_value_info(input_name, dtype, onnx_input_shape)
                body_g.add_model_input(input_name, val)
                i += 1

            log.debug("start preparing body graph outputs nodes")
            new_output_names = []
            for o in body_graph_meta.output_ids:
                # insert identity node, since sometimes we need output same output_id as state_output
                # and scan_out, but ONNX don't allow the same output_id appeared more than once as
                # output node.
                identity_name = utils.make_name("Identity")
                identity_output = utils.port_name(identity_name)
                node = Node(helper.make_node("Identity", [o], [identity_output], name=identity_name), body_g)
                body_g.set_dtype(identity_output, body_g.get_dtype(o))
                body_g.copy_shape(o, identity_output)
                new_output_names.append(identity_output)
                temp_nodes.append(node)

            body_g.set_nodes(temp_nodes)
            body_g.topological_sort(body_g.get_nodes())

            log.debug("start make graph based on body graph nodes")
            body_g.output_names = new_output_names
            graph = body_g.make_graph("scan body graph")
            scan_node.set_attr("body", graph)

        # remove nodes in body graph from g
        for n in set(nodes_to_remove):
            if n in nodes:
                nodes.remove(n)
            elif self.g.is_initializer(n.output[0]):
                del self.g.initializers[n.output[0]]
            else:
                raise ValueError("error when removing nodes")

        return nodes
