"""
Which camera is which: names, and whether each one is plugged in (CAM-2).

**CAM-2, 2026-10-16.** The operator connected an EMEET USB webcam and the
system kept opening the laptop's own camera. `selected_camera.txt` said `1`,
and on this machine DirectShow index 1 is the built-in `ACER HD User Facing`.
OpenCV only ever sees indices, and an index says nothing about the device
behind it - it also shifts when a camera is plugged in or out, so a saved
index can quietly come to mean a different camera.

So this asks Windows directly, and answers two things per DirectShow index:

* **the device's name** - for the logs and the camera picker, so "camera 1"
  can be read as "ACER HD User Facing";
* **whether it is external** - plugged in by the operator rather than built
  into the laptop.

**"External" is Windows' own judgement, not a guess from the name.** Every
device carries `DEVPKEY_Device_InLocalMachineContainer`: True when it is part
of the computer itself, False when it is a separate device attached to it.
Measured on the deployment machine:

| | InLocalMachineContainer | RemovalPolicy |
|---|---|---|
| ACER HD User Facing (built-in, internal USB) | **True** | 1, expect no removal |
| EMEET SmartCam C960 Ultra (USB webcam) | **False** | 3, expect surprise removal |

Both are USB devices, so the bus cannot tell them apart and neither can any
name pattern that would hold on a different laptop. A device with no hardware
path at all - OBS Virtual Camera, DroidCam's driver - has no device node to
ask, and is reported as not external: it may be a perfectly good source, but
it is not "the webcam that is connected".

**The index order is the one OpenCV uses.** `cv2.VideoCapture(i, CAP_DSHOW)`
opens the i-th device of DirectShow's video-input category, enumerated through
`ICreateDevEnum`. This enumerates the same category through the same
interface, so position i here is index i there.

Everything here is best effort. Off Windows, or on any failure, the answer is
an empty list and `camera_utils` falls back to scanning indices exactly as it
did before - a camera must never become unopenable because a name could not be
read.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CameraDevice:
    index: int
    name: str
    external: bool


def list_camera_devices() -> list[CameraDevice]:
    """Every DirectShow video input, in OpenCV's index order. [] if unknown."""
    if sys.platform != "win32":
        return []

    try:
        devices = [
            CameraDevice(index, name, _is_external(path))
            for index, (name, path) in enumerate(_directshow_devices())
        ]
    except Exception:
        logger.warning("Could not list camera devices by name", exc_info=True)
        return []

    logger.debug("Camera devices: %s", devices)
    return devices


# =====================================================
# DIRECTSHOW ENUMERATION
#
# Plain ctypes against the COM vtables, so nothing new has to be installed on
# the deployment machine. Each call is `vtable[slot]`; the slot numbers are the
# method's position in the interface, counting IUnknown's three first.
# =====================================================

_ole32 = ctypes.OleDLL("ole32") if sys.platform == "win32" else None
_oleaut32 = ctypes.WinDLL("oleaut32") if sys.platform == "win32" else None

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106: COM already set up, differently
_VT_BSTR = 8

# IUnknown
_RELEASE = 2
# ICreateDevEnum
_CREATE_CLASS_ENUMERATOR = 3
# IEnumMoniker
_NEXT = 3
# IMoniker: IUnknown 0-2, IPersist 3, IPersistStream 4-7, then BindToObject 8
_BIND_TO_STORAGE = 9
# IPropertyBag
_READ = 3


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text):
        guid = cls()
        _ole32.CLSIDFromString(text, ctypes.byref(guid))
        return guid


class _VARIANT(ctypes.Structure):
    # 24 bytes on 64-bit Windows. Only a BSTR is ever read out of it, which sits
    # at the start of the union.
    _fields_ = [
        ("vt", wintypes.USHORT),
        ("reserved", wintypes.WORD * 3),
        ("bstr", ctypes.c_void_p),
        ("padding", ctypes.c_void_p),
    ]


_CLSID_SYSTEM_DEVICE_ENUM = "{62BE5D10-60EB-11D0-BD3B-00A0C911CE86}"
_IID_ICREATE_DEV_ENUM = "{29840822-5B84-11D0-BD3B-00A0C911CE86}"
_CLSID_VIDEO_INPUT_DEVICE_CATEGORY = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"
_IID_IPROPERTY_BAG = "{55272A00-42CB-11CE-8135-00AA004BB851}"


def _method(interface, slot, *argtypes):
    """Method `slot` of a COM interface pointer, ready to call."""
    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    function = prototype(vtable[slot])
    return lambda *args: function(interface, *args)


def _release(interface):
    if interface:
        _method(interface, _RELEASE)()


def _read_string(bag, prop):
    """A string property from an IPropertyBag, or None."""
    value = _VARIANT()
    read = _method(bag, _READ, wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p)

    try:
        read(prop, ctypes.byref(value), None)
    except OSError:
        return None  # not every device has every property

    try:
        if value.vt != _VT_BSTR or not value.bstr:
            return None
        return ctypes.wstring_at(value.bstr)
    finally:
        _oleaut32.VariantClear(ctypes.byref(value))


def _directshow_devices():
    """`[(friendly_name, device_path or None), ...]` in DirectShow order."""
    try:
        _ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        initialised = True
    except OSError as error:
        if error.winerror != _RPC_E_CHANGED_MODE:
            raise
        initialised = False  # this thread already has COM; use it as it is

    dev_enum = ctypes.c_void_p()
    enum_moniker = ctypes.c_void_p()
    devices = []

    try:
        _ole32.CoCreateInstance(
            ctypes.byref(_GUID.parse(_CLSID_SYSTEM_DEVICE_ENUM)),
            None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_GUID.parse(_IID_ICREATE_DEV_ENUM)),
            ctypes.byref(dev_enum),
        )

        create = _method(
            dev_enum, _CREATE_CLASS_ENUMERATOR,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
        )
        # S_FALSE with no enumerator means the category is empty: no cameras.
        create(
            ctypes.byref(_GUID.parse(_CLSID_VIDEO_INPUT_DEVICE_CATEGORY)),
            ctypes.byref(enum_moniker),
            0,
        )

        if not enum_moniker:
            return devices

        next_moniker = _method(
            enum_moniker, _NEXT, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p
        )
        iid_bag = _GUID.parse(_IID_IPROPERTY_BAG)

        while True:
            moniker = ctypes.c_void_p()
            fetched = wintypes.ULONG()

            if next_moniker(1, ctypes.byref(moniker), ctypes.byref(fetched)) != 0:
                break  # S_FALSE: no more devices

            bag = ctypes.c_void_p()

            try:
                _method(
                    moniker, _BIND_TO_STORAGE,
                    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                )(None, None, ctypes.byref(iid_bag), ctypes.byref(bag))

                devices.append(
                    (
                        _read_string(bag, "FriendlyName") or "Unknown camera",
                        _read_string(bag, "DevicePath"),
                    )
                )
            finally:
                _release(bag)
                _release(moniker)

        return devices

    finally:
        _release(enum_moniker)
        _release(dev_enum)

        if initialised:
            _ole32.CoUninitialize()


# =====================================================
# BUILT IN, OR PLUGGED IN?
# =====================================================

_CR_SUCCESS = 0
_DEVPROP_TYPE_BOOLEAN = 0x11


class _DEVPROPKEY(ctypes.Structure):
    _fields_ = [("fmtid", _GUID), ("pid", wintypes.ULONG)]


def _is_external(device_path):
    """
    True when Windows says this device is not part of the computer itself.

    `device_path` is DirectShow's `\\\\?\\usb#vid_328f&pid_00f1&mi_00#6&...#{guid}\\global`.
    The part between `\\\\?\\` and the interface GUID is the device instance ID
    with `\\` written as `#`, which is what Configuration Manager looks up.
    """
    if not device_path or not device_path.startswith("\\\\?\\"):
        return False  # virtual: no hardware behind it

    instance_id = device_path[4:].split("#{", 1)[0].replace("#", "\\")

    cfgmgr = ctypes.WinDLL("cfgmgr32")
    devnode = wintypes.DWORD()

    if cfgmgr.CM_Locate_DevNodeW(ctypes.byref(devnode), instance_id, 0) != _CR_SUCCESS:
        return False

    key = _DEVPROPKEY(_GUID.parse("{8C7ED206-3F8A-4827-B3AB-AE9E1FAEFC6C}"), 4)
    prop_type = wintypes.ULONG()
    value = ctypes.c_ubyte()
    size = wintypes.ULONG(ctypes.sizeof(value))

    result = cfgmgr.CM_Get_DevNode_PropertyW(
        devnode, ctypes.byref(key), ctypes.byref(prop_type),
        ctypes.byref(value), ctypes.byref(size), 0,
    )

    if result != _CR_SUCCESS or prop_type.value != _DEVPROP_TYPE_BOOLEAN:
        return False

    # DEVPROP_TRUE is 0xFF. In the computer's own container means built in.
    return value.value == 0
