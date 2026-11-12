"""Headless test host for the installed PipeWire SPA filter-graph engine.

No daemon, sound device, user session, or synthetic DSP substitute is used.
The small ctypes declarations follow PipeWire's version-0 public SPA ABI:
https://github.com/PipeWire/pipewire/blob/1.4.2/spa/include/spa/filter-graph/filter-graph.h
https://github.com/PipeWire/pipewire/blob/1.4.2/spa/include/spa/support/plugin-loader.h
https://github.com/PipeWire/pipewire/blob/1.4.2/src/pipewire/pipewire.h
This is a test utility, not the real-time audio host shipped on the tablet.
"""
import ctypes as C
from ctypes.util import find_library
import json

import numpy as np

U32, VOID, STRING, INT = C.c_uint32, C.c_void_p, C.c_char_p, C.c_int

class DictItem(C.Structure):
    _fields_ = [('key', STRING), ('value', STRING)]

class Dict(C.Structure):
    _fields_ = [('flags', U32), ('n_items', U32), ('items', C.POINTER(DictItem))]

class Support(C.Structure):
    _fields_ = [('type', STRING), ('data', VOID)]

class Callbacks(C.Structure):
    _fields_ = [('funcs', VOID), ('data', VOID)]

class Interface(C.Structure):
    _fields_ = [('type', STRING), ('version', U32), ('cb', Callbacks)]

GET_INTERFACE = C.CFUNCTYPE(INT, VOID, STRING, C.POINTER(VOID))
CLEAR = C.CFUNCTYPE(INT, VOID)

class Handle(C.Structure):
    _fields_ = [('version', U32), ('get_interface', GET_INTERFACE), ('clear', CLEAR)]

LOAD = C.CFUNCTYPE(VOID, VOID, STRING, C.POINTER(Dict))
UNLOAD = C.CFUNCTYPE(INT, VOID, VOID)

class LoaderMethods(C.Structure):
    _fields_ = [('version', U32), ('load', LOAD), ('unload', UNLOAD)]

ACTIVATE = C.CFUNCTYPE(INT, VOID, C.POINTER(Dict))
SIMPLE = C.CFUNCTYPE(INT, VOID)
PROCESS = C.CFUNCTYPE(INT, VOID, C.POINTER(VOID), C.POINTER(VOID), U32)

class GraphMethods(C.Structure):
    _fields_ = [
        ('version', U32), ('add_listener', VOID), ('enum_prop_info', VOID),
        ('get_props', VOID), ('set_props', VOID), ('activate', ACTIVATE),
        ('deactivate', SIMPLE), ('reset', SIMPLE), ('process', PROCESS),
    ]

def dictionary(values):
    items = (DictItem * len(values))(
        *[DictItem(k.encode(), str(v).encode()) for k, v in values.items()])
    return Dict(0, len(items), items)

class Graph:
    def __init__(self, graph, rate=48000, quantum=1024):
        self.rate, self.quantum = rate, quantum
        self.handle = None
        self.closed = False
        library = find_library('pipewire-0.3')
        if not library:
            raise RuntimeError('Installed libpipewire-0.3 is required; no mock fallback')
        self.pw = C.CDLL(library, use_errno=True)
        self.pw.pw_init.argtypes = [VOID, VOID]
        self.pw.pw_init.restype = None
        self.pw.pw_deinit.argtypes = []
        self.pw.pw_deinit.restype = None
        self.pw.pw_get_library_version.restype = STRING
        self.version = self.pw.pw_get_library_version().decode()
        self.pw.pw_get_support.argtypes = [C.POINTER(Support), U32]
        self.pw.pw_get_support.restype = U32
        self.pw.pw_load_spa_handle.argtypes = [
            STRING, STRING, C.POINTER(Dict), U32, C.POINTER(Support)]
        self.pw.pw_load_spa_handle.restype = VOID
        self.pw.pw_unload_spa_handle.argtypes = [VOID]
        self.pw.pw_unload_spa_handle.restype = INT
        self.pw.pw_init(None, None)
        try:
            self._load(graph)
        except BaseException:
            self.close()
            raise

    def _load(self, graph):
        self.support = (Support * 32)()
        self.n_support = self.pw.pw_get_support(self.support, 31)
        self.load_callback = LOAD(self._load_plugin)
        self.unload_callback = UNLOAD(
            lambda obj, handle: self.pw.pw_unload_spa_handle(handle))
        self.loader_methods = LoaderMethods(0, self.load_callback, self.unload_callback)
        self.loader = Interface(
            b'Spa:Pointer:Interface:PluginLoader', 0,
            Callbacks(C.cast(C.pointer(self.loader_methods), VOID), None))
        self.support[self.n_support] = Support(
            self.loader.type, C.cast(C.pointer(self.loader), VOID))
        self.n_support += 1
        props = dictionary({
            'clock.quantum-limit': self.quantum,
            'filter-graph.n_inputs': 2,
            'filter-graph.n_outputs': 2,
            'filter.graph': json.dumps(graph),
        })
        self.handle = self.pw.pw_load_spa_handle(
            b'filter-graph/libspa-filter-graph', b'filter.graph',
            C.byref(props), self.n_support, self.support)
        if not self.handle:
            raise OSError(C.get_errno(), 'Load installed SPA filter graph')
        iface = VOID()
        handle = C.cast(self.handle, C.POINTER(Handle)).contents
        self._check(handle.get_interface(
            self.handle, b'Spa:Pointer:Interface:FilterGraph', C.byref(iface)),
            'Get filter graph interface')
        self.iface = C.cast(iface, C.POINTER(Interface)).contents
        self.methods = C.cast(self.iface.cb.funcs, C.POINTER(GraphMethods)).contents
        self.activate()

    def _load_plugin(self, obj, name, info):
        library = None
        if info:
            for item in info.contents.items[:info.contents.n_items]:
                if item.key == b'library.name':
                    library = item.value
        return self.pw.pw_load_spa_handle(
            library, name, info, self.n_support, self.support)

    @staticmethod
    def _check(result, action):
        if result < 0:
            raise OSError(-result, action)

    def activate(self):
        props = dictionary({'audio.rate': self.rate})
        self._check(self.methods.activate(self.iface.cb.data, C.byref(props)),
                    'Activate filter graph')

    def deactivate(self):
        self._check(self.methods.deactivate(self.iface.cb.data), 'Deactivate graph')

    def reset(self):
        self._check(self.methods.reset(self.iface.cb.data), 'Reset filter graph')

    def process(self, samples, blocksize=None):
        if self.closed:
            raise RuntimeError('Filter graph is closed')
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 2 or samples.shape[1] != 2:
            raise ValueError('Expected frames by two channels')
        blocksize = blocksize or self.quantum
        if not 0 < blocksize <= self.quantum:
            raise ValueError('Block size exceeds the graph allocation')
        output = np.empty_like(samples)
        for start in range(0, len(samples), blocksize):
            inputs = np.ascontiguousarray(samples[start:start + blocksize].T)
            outputs = np.empty_like(inputs)
            input_ptrs = (VOID * 2)(*[int(channel.ctypes.data) for channel in inputs])
            output_ptrs = (VOID * 2)(*[int(channel.ctypes.data) for channel in outputs])
            self._check(self.methods.process(
                self.iface.cb.data, input_ptrs, output_ptrs, len(inputs[0])),
                'Process filter graph')
            output[start:start + blocksize] = outputs.T
        return output

    def close(self):
        if not self.closed:
            if self.handle:
                self.pw.pw_unload_spa_handle(self.handle)
            self.pw.pw_deinit()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

def stock_graph():
    return {
        'nodes': [{'type': 'builtin', 'name': f'hp{i}', 'label': 'bq_highpass',
                   'control': {'Freq': 80, 'Q': .70710678}} for i in range(2)],
        'inputs': ['hp0:In', 'hp1:In'],
        'outputs': ['hp0:Out', 'hp1:Out'],
    }

if __name__ == '__main__':
    with Graph(stock_graph()) as graph:
        print('PipeWire SPA engine version:', graph.version)
        output = graph.process(np.full((48000, 2), .1, np.float32))
        print('Stock DC-step first/last samples:', output[0], output[-1])
