# automatically generated by the FlatBuffers compiler, do not modify

# namespace: tflite

import flatbuffers
from flatbuffers.compat import import_numpy
np = import_numpy()

class LessOptions(object):
    __slots__ = ['_tab']

    @classmethod
    def GetRootAsLessOptions(cls, buf, offset):
        n = flatbuffers.encode.Get(flatbuffers.packer.uoffset, buf, offset)
        x = LessOptions()
        x.Init(buf, n + offset)
        return x

    @classmethod
    def LessOptionsBufferHasIdentifier(cls, buf, offset, size_prefixed=False):
        return flatbuffers.util.BufferHasIdentifier(buf, offset, b"\x54\x46\x4C\x33", size_prefixed=size_prefixed)

    # LessOptions
    def Init(self, buf, pos):
        self._tab = flatbuffers.table.Table(buf, pos)

def LessOptionsStart(builder): builder.StartObject(0)
def LessOptionsEnd(builder): return builder.EndObject()
