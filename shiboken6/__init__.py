__version__ = "6.11.1"
__version_info__ = (6, 11, 1, "", "")
__minimum_python_version__ = (3, 10)
__maximum_python_version__ = (3, 14)

# PYSIDE-932: Python 2 cannot import 'zipfile' for embedding while being imported, itself.
# We simply pre-load all imports for the signature extension.
# Also, PyInstaller seems not always to be reliable in finding modules.
# We explicitly import everything that is needed:

from shiboken6.Shiboken import (
    Object,
    VoidPtr,
    createdByPython,
    delete,
    disassembleFrame,
    dump,
    dumpConverters,
    dumpTypeGraph,
    dumpWrapperMap,
    getAllValidWrappers,
    getCppPointer,
    invalidate,
    isValid,
    ownedByPython,
    replaceModuleDict,
    wrapInstance,
)

__all__ = [
    "Object",
    "VoidPtr",
    "createdByPython",
    "delete",
    "disassembleFrame",
    "dump",
    "dumpConverters",
    "dumpTypeGraph",
    "dumpWrapperMap",
    "getAllValidWrappers",
    "getCppPointer",
    "invalidate",
    "isValid",
    "ownedByPython",
    "replaceModuleDict",
    "wrapInstance",
]
