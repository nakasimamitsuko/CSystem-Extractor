#!/usr/bin/env python3
"""
Cyberworks C,system 引擎 DAT 存档工具 (Python版)
精确移植自 CSystemTools (https://github.com/arcusmaximus/CSystemTools)

存档结构:
  索引文件 (Arc01/02/03/07/09.dat):
    BCD头部(16字节) + LZSS压缩的索引条目列表
  内容文件 (Arc04/05/06/08/10.dat):
    裸数据, 通过索引的 offset+size 定位
    类型 b/c/e/n/o/p 的内容做了LZSS压缩

索引条目格式 (每条 0x1B 或 0x1C 字节):
  int32  Version
  int32  Id
  int32  UncompressedSize
  int32  CompressedSize
  int32  Offset
  byte   Type         ('a','b','c','d','e'...)
  byte   SubType      ('0' 等)
  int32  Reserved     (写入时填 0xFFFFFFFF)
  byte   ContentArchiveIndex  (仅 version > 0x16)

图像类型 (Type='b'):
  子格式 'c': byte'c' + 4字节BE大小 + 标准图像(BMP/JPG/PNG)
  子格式 'a': 自定义位图(完整+alpha), AttrValue编码参数
  子格式 'd': delta差分图(基于另一张图)
"""

import sys
import struct
import os
import io
import argparse
import ctypes
import ctypes.util
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ============================================================
# C 加速库自动检测
# ============================================================

_lzss_lib = None

def _load_native_lib():
    global _lzss_lib
    if _lzss_lib is not None:
        return _lzss_lib

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if sys.platform == 'win32':
        candidates = [
            os.path.join(script_dir, 'lzss_fast.dll'),
            'lzss_fast.dll',
        ]
    else:
        candidates = [
            os.path.join(script_dir, 'lzss_fast.so'),
            'lzss_fast.so',
        ]

    for path in candidates:
        try:
            lib = ctypes.CDLL(path)
            lib.lzss_decompress_c.argtypes = [
                ctypes.c_char_p, ctypes.c_int,
                ctypes.c_char_p, ctypes.c_int
            ]
            lib.lzss_decompress_c.restype = ctypes.c_int
            lib.lzss_compress_c.argtypes = [
                ctypes.c_char_p, ctypes.c_int,
                ctypes.c_char_p, ctypes.c_int
            ]
            lib.lzss_compress_c.restype = ctypes.c_int
            _lzss_lib = lib
            return lib
        except (OSError, AttributeError):
            continue
    return None

HAS_NATIVE = _load_native_lib() is not None
if HAS_NATIVE:
    print("[加速] 已加载 lzss_fast 原生库")
else:
    print("[提示] 未找到 lzss_fast 原生库, 使用纯 Python (运行 python build_lzss.py 编译加速库)")


# ============================================================
# BCD 编解码 (XOR 0x7F, 8字节十进制)
# ============================================================

def bcd_read(stream: io.BufferedIOBase) -> int:
    value = 0
    for _ in range(8):
        value *= 10
        b = stream.read(1)
        if len(b) == 0:
            raise EOFError("BCD read: unexpected end of stream")
        b = b[0]
        if b != 0xFF:
            value += b ^ 0x7F
    return value


def bcd_write(stream: io.BufferedIOBase, value: int):
    bcd = bytearray(8)
    for i in range(7, -1, -1):
        value, remainder = divmod(value, 10)
        bcd[i] = (remainder ^ 0x7F) if remainder != 0 else 0xFF
    stream.write(bcd)


# ============================================================
# LZSS 压缩/解压
# ============================================================

WINDOW_SIZE = 0x1000
MAX_MATCH_LENGTH = 18
MATCH_THRESHOLD = 2
CHAR_FILLER = 0x00


def lzss_decompress(src: bytes, decompressed_size: int) -> bytes:
    lib = _load_native_lib()
    if lib is not None:
        dst = ctypes.create_string_buffer(decompressed_size)
        ret = lib.lzss_decompress_c(src, len(src), dst, decompressed_size)
        if ret >= 0:
            return dst.raw[:ret]

    return _lzss_decompress_py(src, decompressed_size)


def lzss_compress(data: bytes) -> bytes:
    lib = _load_native_lib()
    if lib is not None:
        max_out = len(data) * 2 + 1024
        dst = ctypes.create_string_buffer(max_out)
        ret = lib.lzss_compress_c(data, len(data), dst, max_out)
        if ret >= 0:
            return dst.raw[:ret]

    return _lzss_compress_py(data)


def _lzss_decompress_py(src: bytes, decompressed_size: int) -> bytes:
    ring = bytearray([CHAR_FILLER] * WINDOW_SIZE)
    ring_pos = WINDOW_SIZE - MAX_MATCH_LENGTH
    output = bytearray()
    src_pos = 0
    src_len = len(src)
    flags = 0
    out_count = 0

    while out_count < decompressed_size:
        flags >>= 1
        if (flags & 0x100) == 0:
            if src_pos >= src_len:
                break
            flags = src[src_pos] | 0xFF00
            src_pos += 1

        if flags & 1:
            if src_pos >= src_len:
                break
            b = src[src_pos]
            src_pos += 1
            output.append(b)
            ring[ring_pos] = b
            ring_pos = (ring_pos + 1) & (WINDOW_SIZE - 1)
            out_count += 1
        else:
            if src_pos + 1 >= src_len:
                break
            low = src[src_pos]
            high = src[src_pos + 1]
            src_pos += 2

            offset = low | ((high & 0xF0) << 4)
            length = (high & 0x0F) + MATCH_THRESHOLD + 1

            for i in range(length):
                if out_count >= decompressed_size:
                    break
                b = ring[(offset + i) & (WINDOW_SIZE - 1)]
                output.append(b)
                ring[ring_pos] = b
                ring_pos = (ring_pos + 1) & (WINDOW_SIZE - 1)
                out_count += 1

    return bytes(output)


def _lzss_compress_py(data: bytes) -> bytes:
    """LZSS压缩, 简单匹配实现"""
    ring = bytearray([CHAR_FILLER] * WINDOW_SIZE)
    ring_pos = WINDOW_SIZE - MAX_MATCH_LENGTH
    output = bytearray()
    src_pos = 0
    src_len = len(data)

    while src_pos < src_len:
        flag_byte = 0
        flag_pos = len(output)
        output.append(0)
        items = bytearray()

        for bit in range(8):
            if src_pos >= src_len:
                break

            best_offset = 0
            best_length = 0
            max_len = min(MAX_MATCH_LENGTH, src_len - src_pos)

            if max_len > MATCH_THRESHOLD:
                for offset in range(WINDOW_SIZE):
                    match_len = 0
                    while match_len < max_len:
                        if data[src_pos + match_len] != ring[(offset + match_len) & (WINDOW_SIZE - 1)]:
                            break
                        match_len += 1
                    if match_len > best_length:
                        best_length = match_len
                        best_offset = offset
                        if best_length == max_len:
                            break

            if best_length > MATCH_THRESHOLD:
                low = best_offset & 0xFF
                high = ((best_offset >> 4) & 0xF0) | ((best_length - MATCH_THRESHOLD - 1) & 0x0F)
                items.append(low)
                items.append(high)
                for _ in range(best_length):
                    ring[ring_pos] = data[src_pos]
                    ring_pos = (ring_pos + 1) & (WINDOW_SIZE - 1)
                    src_pos += 1
            else:
                flag_byte |= (1 << bit)
                b = data[src_pos]
                items.append(b)
                ring[ring_pos] = b
                ring_pos = (ring_pos + 1) & (WINDOW_SIZE - 1)
                src_pos += 1

        output[flag_pos] = flag_byte
        output.extend(items)

    return bytes(output)


# ============================================================
# BCD 压缩/解压 (索引文件的头部+LZSS包装)
# ============================================================

def bcd_decompress(stream) -> bytes:
    uncompressed_size = bcd_read(stream)
    compressed_size = bcd_read(stream)
    offset = stream.tell() if hasattr(stream, 'tell') else 0
    compressed_data = stream.read(compressed_size)
    return lzss_decompress(compressed_data, uncompressed_size)


def bcd_compress(data: bytes) -> bytes:
    compressed = lzss_compress(data)
    buf = io.BytesIO()
    bcd_write(buf, len(data))
    bcd_write(buf, len(compressed))
    buf.write(compressed)
    return buf.getvalue()


# ============================================================
# ArchiveEntry (索引条目)
# ============================================================

class ArchiveEntry:
    def __init__(self):
        self.version = 0
        self.index = 0
        self.content_archive_index = 0
        self.id = 0
        self.offset = 0
        self.uncompressed_size = 0
        self.compressed_size = 0
        self.type = '\0'
        self.sub_type = '\0'

    def read(self, f):
        self.version = struct.unpack('<i', f.read(4))[0]
        self.id = struct.unpack('<i', f.read(4))[0]
        self.uncompressed_size = struct.unpack('<i', f.read(4))[0]
        self.compressed_size = struct.unpack('<i', f.read(4))[0]
        self.offset = struct.unpack('<i', f.read(4))[0]
        self.type = chr(f.read(1)[0])
        self.sub_type = chr(f.read(1)[0])
        f.read(4)  # reserved
        if self.version > 0x16:
            b = f.read(1)
            self.content_archive_index = b[0] if b else 0

    def write(self, f):
        f.write(struct.pack('<i', self.version))
        f.write(struct.pack('<i', self.id))
        f.write(struct.pack('<i', self.uncompressed_size))
        f.write(struct.pack('<i', self.compressed_size))
        f.write(struct.pack('<i', self.offset))
        f.write(bytes([ord(self.type)]))
        f.write(bytes([ord(self.sub_type)]))
        f.write(struct.pack('<i', -1))  # reserved = 0xFFFFFFFF
        if self.version > 0x16:
            f.write(bytes([self.content_archive_index]))

    @property
    def entry_size(self):
        return 0x1C if self.version > 0x16 else 0x1B

    def __repr__(self):
        return (f"Entry(id={self.id}, type='{self.type}{self.sub_type}', "
                f"offset=0x{self.offset:X}, usize={self.uncompressed_size}, "
                f"csize={self.compressed_size}, arc={self.content_archive_index})")


COMPRESSED_TYPES = set('bcenop')


# ============================================================
# ArchiveReader
# ============================================================

class ArchiveReader:
    def __init__(self, index_path: str, content_paths: list):
        self.content_streams = [open(p, 'rb') for p in content_paths]
        self.entries = []
        self.entries_by_type = {}

        with open(index_path, 'rb') as f:
            index_data = bcd_decompress(f)

        stream = io.BytesIO(index_data)
        while stream.tell() < len(index_data):
            entry = ArchiveEntry()
            entry.read(stream)
            entry.index = len(self.entries_by_type.get(entry.type, []))
            self.entries_by_type.setdefault(entry.type, []).append(entry)
            self.entries.append(entry)

    def get_entry_content(self, entry: ArchiveEntry) -> bytes:
        stream = self.content_streams[entry.content_archive_index]
        stream.seek(entry.offset)

        if entry.compressed_size == entry.uncompressed_size:
            return stream.read(entry.uncompressed_size)
        else:
            compressed = stream.read(entry.compressed_size)
            return lzss_decompress(compressed, entry.uncompressed_size)

    def close(self):
        for s in self.content_streams:
            s.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ============================================================
# ArchiveWriter
# ============================================================

class ArchiveWriter:
    def __init__(self, version: int, index_path: str, content_paths: list):
        self.version = version
        self.index_path = index_path
        self.content_streams = [open(p, 'wb') for p in content_paths]
        self.entries = []

    def write_entry(self, id: int, type_char: str, sub_type: str, content: bytes):
        # 选择内容文件
        content_archive_index = 0
        entry = ArchiveEntry()
        entry.version = self.version
        entry.content_archive_index = content_archive_index
        entry.id = id
        entry.type = type_char
        entry.sub_type = sub_type
        entry.uncompressed_size = len(content)

        stream = self.content_streams[content_archive_index]
        entry.offset = stream.tell()

        if type_char in COMPRESSED_TYPES:
            compressed = lzss_compress(content)
            stream.write(compressed)
            entry.compressed_size = len(compressed)
        else:
            stream.write(content)
            entry.compressed_size = len(content)

        self.entries.append(entry)

    def close(self):
        # 写入索引
        index_buf = io.BytesIO()
        for entry in sorted(self.entries, key=lambda e: e.id):
            entry.write(index_buf)

        compressed_index = bcd_compress(index_buf.getvalue())
        with open(self.index_path, 'wb') as f:
            f.write(compressed_index)

        for s in self.content_streams:
            s.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ============================================================
# CSystem 图像处理
# ============================================================

ATTR_VALUE_BASE = 0xE9
ATTR_VALUE_END = 0xEF
ATTR_VALUE_ZERO = 0xFB


def read_attr_value(stream) -> int:
    b = stream.read(1)[0]
    units = b if b != ATTR_VALUE_ZERO else 0

    hundreds = 0
    tens = 0
    while True:
        b = stream.read(1)[0]
        if b == ATTR_VALUE_END:
            break
        if b == ATTR_VALUE_BASE:
            hundreds += 1
        else:
            tens = b if b != ATTR_VALUE_ZERO else 0

    return (hundreds * ATTR_VALUE_BASE + tens) * ATTR_VALUE_BASE + units


def write_attr_value(stream, value: int):
    units_q, units = divmod(value, ATTR_VALUE_BASE)
    hundreds, tens = divmod(units_q, ATTR_VALUE_BASE)

    stream.write(bytes([ATTR_VALUE_ZERO if units == 0 else units]))
    if tens != 0:
        stream.write(bytes([tens]))
    for _ in range(hundreds):
        stream.write(bytes([ATTR_VALUE_BASE]))
    stream.write(bytes([ATTR_VALUE_END]))


class CSystemImage:
    def __init__(self, base_index=-1):
        self.base_index = base_index
        self.width = 0
        self.height = 0
        self.mask = None
        self.alpha = None
        self.color = None
        self.standard_image = None

    def read(self, data: bytes):
        stream = io.BytesIO(data)
        type_byte = chr(stream.read(1)[0])

        if type_byte in ('a', 'd'):
            self._read_csystem(stream)
        elif type_byte == 'c':
            self._read_standard_wrapper(stream)
        else:
            raise ValueError(f"Unknown image type: '{type_byte}' (0x{ord(type_byte):02X})")

    def _read_csystem(self, stream):
        stream.read(1)  # skip 1 byte
        self.base_index = read_attr_value(stream)
        read_attr_value(stream)  # field_C
        read_attr_value(stream)  # field_38
        self.width = read_attr_value(stream)
        read_attr_value(stream)  # field_3C
        self.height = read_attr_value(stream)
        read_attr_value(stream)  # field_18
        read_attr_value(stream)  # field_34
        read_attr_value(stream)  # field_1C
        alpha_size = read_attr_value(stream)
        read_attr_value(stream)  # field_2C
        color_size = read_attr_value(stream)
        read_attr_value(stream)  # field_28
        read_attr_value(stream)  # flags
        read_attr_value(stream)  # field_40
        read_attr_value(stream)  # field_44
        read_attr_value(stream)  # field_48
        mask_size = read_attr_value(stream)
        read_attr_value(stream)  # field_4C
        read_attr_value(stream)  # field_8

        if mask_size > 0:
            self.mask = stream.read(mask_size)
        if alpha_size > 0:
            self.alpha = stream.read(alpha_size)
        if color_size > 0:
            self.color = stream.read(color_size)

    def _read_standard_wrapper(self, stream):
        length = 0
        for _ in range(4):
            length = (length << 8) | stream.read(1)[0]
        self.standard_image = stream.read(length)

    def write(self) -> bytes:
        buf = io.BytesIO()
        if self.standard_image is not None:
            self._write_standard_wrapper(buf)
        else:
            self._write_csystem(buf)
        return buf.getvalue()

    def _write_csystem(self, stream):
        flags = 1
        if self.alpha is not None:
            flags |= 6

        stream.write(bytes([ord('d') if self.mask else ord('a')]))
        stream.write(bytes([ATTR_VALUE_END]))
        write_attr_value(stream, self.base_index)
        write_attr_value(stream, 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, self.width)
        write_attr_value(stream, 0)
        write_attr_value(stream, self.height)
        write_attr_value(stream, 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, len(self.alpha) if self.alpha else 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, len(self.color) if self.color else 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, flags)
        write_attr_value(stream, 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, len(self.mask) if self.mask else 0)
        write_attr_value(stream, 0)
        write_attr_value(stream, 0)

        if self.mask:
            stream.write(self.mask)
        if self.alpha:
            stream.write(self.alpha)
        if self.color:
            stream.write(self.color)

    def _write_standard_wrapper(self, stream):
        stream.write(b'c')
        length = len(self.standard_image)
        stream.write(bytes([(length >> 24) & 0xFF,
                            (length >> 16) & 0xFF,
                            (length >> 8) & 0xFF,
                            length & 0xFF]))
        stream.write(self.standard_image)

    def save_as_png(self, filepath: str):
        if not HAS_PIL:
            raise RuntimeError("需要 Pillow: pip install Pillow")

        if self.standard_image is not None:
            img = Image.open(io.BytesIO(self.standard_image))
            img.save(filepath, 'PNG')
            return

        if self.mask is not None:
            raise ValueError("不能直接保存delta图, 需要先合并基础图")

        if not self.color:
            raise ValueError("无 color 数据")

        # 从 color 数据大小反推真实尺寸
        # 引擎的 width/height 字段可能是渲染位置偏移而非实际图片尺寸
        w = self.width
        h = self.height
        row_stride = w * 3 + (w & 3)
        expected = row_stride * h

        if expected > len(self.color) and w > 0:
            # width/height 与 color 数据不匹配，从 color 大小反推
            actual_h = len(self.color) // row_stride
            if actual_h > 0 and row_stride * actual_h == len(self.color):
                h = actual_h
            else:
                # 尝试不带对齐
                if len(self.color) % (w * 3) == 0:
                    h = len(self.color) // (w * 3)
                    row_stride = w * 3
                else:
                    # 最后手段: 从总像素数推算
                    total_pixels = len(self.color) // 3
                    if total_pixels > 0 and w > 0:
                        h = total_pixels // w
                        if h == 0:
                            # w 也不对，尝试正方形
                            import math
                            side = int(math.isqrt(total_pixels))
                            w, h = side, side
                        row_stride = w * 3

        argb = bytearray(w * 4 * h)
        input_offset = 0
        output_offset = 0
        color_len = len(self.color)
        alpha_len = len(self.alpha) if self.alpha else 0

        for y in range(h):
            for x in range(w):
                if input_offset + 2 < color_len:
                    argb[output_offset + 0] = self.color[input_offset + 2]  # R (BGR->RGB)
                    argb[output_offset + 1] = self.color[input_offset + 1]  # G
                    argb[output_offset + 2] = self.color[input_offset + 0]  # B
                else:
                    argb[output_offset + 0] = 0
                    argb[output_offset + 1] = 0
                    argb[output_offset + 2] = 0

                if self.alpha and input_offset < alpha_len:
                    argb[output_offset + 3] = self.alpha[input_offset]
                else:
                    argb[output_offset + 3] = 0xFF

                input_offset += 3
                output_offset += 4
            # 行对齐跳过
            input_offset += (row_stride - w * 3)

        img = Image.frombytes('RGBA', (w, h), bytes(argb))
        img.save(filepath, 'PNG')

    def load_from_png_as_wrapper(self, filepath: str):
        """作为标准图像包装(type 'c')加载"""
        with open(filepath, 'rb') as f:
            self.standard_image = f.read()
        img = Image.open(io.BytesIO(self.standard_image))
        self.width = img.width
        self.height = img.height
        self.mask = None
        self.color = None
        self.alpha = None

    def load_from_png_as_csystem(self, filepath: str):
        """作为CSystem自定义格式(type 'a')加载"""
        if not HAS_PIL:
            raise RuntimeError("需要 Pillow: pip install Pillow")

        img = Image.open(filepath).convert('RGBA')
        self.width = img.width
        self.height = img.height
        row_stride = self.width * 3 + (self.width & 3)

        self.color = bytearray(row_stride * self.height)
        self.alpha = bytearray(row_stride * self.height)
        self.mask = None
        self.standard_image = None
        alpha_needed = False

        pixels = img.load()
        output_offset = 0
        for y in range(self.height):
            for x in range(self.width):
                r, g, b, a = pixels[x, y]
                self.color[output_offset + 0] = b  # BGR
                self.color[output_offset + 1] = g
                self.color[output_offset + 2] = r
                self.alpha[output_offset + 0] = a
                self.alpha[output_offset + 1] = a
                self.alpha[output_offset + 2] = a
                if a != 0xFF:
                    alpha_needed = True
                output_offset += 3
            output_offset += self.width & 3

        if not alpha_needed:
            self.alpha = None

    def convert_delta_to_full(self, base_image: 'CSystemImage'):
        if self.mask is None:
            raise ValueError("不是delta图")
        self.width = base_image.width
        self.height = base_image.height
        row_stride = self.width * 3 + (self.width & 3)
        merged_color = bytearray(row_stride * self.height)
        merged_alpha = bytearray(row_stride * self.height) if base_image.alpha else None

        full_offset = 0
        mask_offset = 0
        mask_bit = 1
        delta_color_offset = 0
        delta_alpha_offset = 0

        for y in range(self.height):
            for x in range(self.width):
                if self.mask[mask_offset] & mask_bit:
                    merged_color[full_offset:full_offset+3] = self.color[delta_color_offset:delta_color_offset+3]
                    if merged_alpha is not None:
                        a = self.alpha[delta_alpha_offset] if self.alpha else base_image.alpha[full_offset]
                        merged_alpha[full_offset] = a
                        merged_alpha[full_offset+1] = a
                        merged_alpha[full_offset+2] = a
                    delta_color_offset += 3
                    delta_alpha_offset += 1
                else:
                    merged_color[full_offset:full_offset+3] = base_image.color[full_offset:full_offset+3]
                    if merged_alpha is not None:
                        merged_alpha[full_offset] = base_image.alpha[full_offset]
                        merged_alpha[full_offset+1] = base_image.alpha[full_offset]
                        merged_alpha[full_offset+2] = base_image.alpha[full_offset]

                full_offset += 3
                mask_bit <<= 1
                if mask_bit == 0x100:
                    mask_offset += 1
                    mask_bit = 1
            full_offset += self.width & 3

        self.mask = None
        self.color = bytes(merged_color)
        self.alpha = bytes(merged_alpha) if merged_alpha else None


# ============================================================
# 文件名约定 (与CSystemTools一致)
# ============================================================

def get_raw_filename(id: int, type_char: str, sub_type: str) -> str:
    return f"{id:06d}.{type_char}{sub_type}"


def get_image_filename(id: int) -> str:
    return f"{id:06d}.png"


def get_image_foldername(id: int) -> str:
    return f"{id:06d}"


def parse_raw_filename(filename: str):
    import re
    m = re.match(r'^(\d+)\.(\w)(\w)?$', filename)
    if not m:
        return None
    return int(m.group(1)), m.group(2), m.group(3) or '0'


# ============================================================
# Unpack
# ============================================================

def unpack(index_path: str, content_paths: list, output_dir: str):
    os.makedirs(output_dir, exist_ok=True)

    with ArchiveReader(index_path, content_paths) as reader:
        if reader.entries:
            print(f"Archive version: {reader.entries[0].version}")

        # 按类型分组处理图像
        b_entries = [e for e in reader.entries if e.type == 'b']
        other_entries = [e for e in reader.entries if e.type != 'b']

        # 先解包非图像文件
        for entry in other_entries:
            try:
                content = reader.get_entry_content(entry)
                fname = get_raw_filename(entry.id, entry.type, entry.sub_type)
                with open(os.path.join(output_dir, fname), 'wb') as f:
                    f.write(content)
                print(f"  解包 {entry.id:06d} (type {entry.type}{entry.sub_type})")
            except Exception as e:
                print(f"  [错误] {entry.id:06d}: {e}")

        # 解包图像: 先读取所有图像到内存，再处理 delta 合并
        if b_entries:
            _unpack_images(reader, b_entries, output_dir)

    print(f"完成, 输出到 {output_dir}")


def _unpack_images(reader, b_entries, output_dir):
    """解包所有图像，正确处理 delta 合并"""
    # 第一遍: 读取所有图像数据到内存
    images = {}  # index -> CSystemImage
    entry_map = {}  # index -> entry

    for entry in b_entries:
        try:
            content = reader.get_entry_content(entry)
            img = CSystemImage()
            img.read(content)
            images[entry.index] = img
            entry_map[entry.index] = entry
        except Exception as e:
            print(f"  [错误] {entry.id:06d}: 读取失败 - {e}")

    # 第二遍: 处理完整图和 delta 图
    saved_count = 0
    for entry in b_entries:
        if entry.index not in images:
            continue

        img = images[entry.index]

        try:
            if img.mask is not None and img.base_index != entry.index:
                # delta 图 — 需要合并到基础图上
                if img.base_index not in images:
                    print(f"  [警告] {entry.id:06d}: 基础图 index={img.base_index} 不存在")
                    # 保存为不完整图（标注 delta）
                    img.save_as_png(os.path.join(output_dir, f"{entry.id:06d}_delta.png"))
                    print(f"  解包 {entry.id:06d} (type b0, delta 未合并)")
                    saved_count += 1
                    continue

                base_img = images[img.base_index]
                base_entry = entry_map[img.base_index]

                # 确保基础图是完整图
                if base_img.mask is not None:
                    print(f"  [警告] {entry.id:06d}: 基础图 {base_entry.id:06d} 也是 delta 图")
                    img.save_as_png(os.path.join(output_dir, f"{entry.id:06d}_delta.png"))
                    saved_count += 1
                    continue

                # 基础图放在以其 id 命名的文件夹中
                base_folder = os.path.join(output_dir, get_image_foldername(base_entry.id))
                os.makedirs(base_folder, exist_ok=True)

                # 确保基础图已保存到文件夹中
                base_png = os.path.join(base_folder, get_image_filename(base_entry.id))
                base_in_root = os.path.join(output_dir, get_image_filename(base_entry.id))
                if os.path.exists(base_in_root) and not os.path.exists(base_png):
                    os.rename(base_in_root, base_png)
                if not os.path.exists(base_png):
                    base_img.save_as_png(base_png)

                # 合并 delta
                img.convert_delta_to_full(base_img)
                img.save_as_png(os.path.join(base_folder, get_image_filename(entry.id)))
                print(f"  解包 {entry.id:06d} (type b0, delta -> {base_entry.id:06d})")
            else:
                # 完整图或标准包装图
                img.save_as_png(os.path.join(output_dir, get_image_filename(entry.id)))
                print(f"  解包 {entry.id:06d} (type b0)")

            saved_count += 1
        except Exception as e:
            print(f"  [错误] {entry.id:06d}: {e}")
            # 保存原始数据供调试
            try:
                content = reader.get_entry_content(entry)
                raw_path = os.path.join(output_dir, get_raw_filename(entry.id, 'b', '0'))
                with open(raw_path, 'wb') as f:
                    f.write(content)
                print(f"         已保存原始数据: {raw_path}")
            except:
                pass

    print(f"  图像: {saved_count}/{len(b_entries)} 完成")


# ============================================================
# Pack
# ============================================================

def pack(version: int, input_dir: str, index_path: str, content_paths: list):
    with ArchiveWriter(version, index_path, content_paths) as writer:
        # 先打包非图像文件
        for fname in sorted(os.listdir(input_dir)):
            fpath = os.path.join(input_dir, fname)
            if not os.path.isfile(fpath):
                continue
            parsed = parse_raw_filename(fname)
            if not parsed:
                continue
            id, type_char, sub_type = parsed
            if type_char == 'b':
                continue  # 图像单独处理
            print(f"  封包 {id:06d} (type {type_char}{sub_type})")
            with open(fpath, 'rb') as f:
                content = f.read()
            writer.write_entry(id, type_char, sub_type, content)

        # 打包图像
        _pack_images(input_dir, writer)

    print(f"完成, 索引: {index_path}")


def _pack_images(input_dir: str, writer):
    import re

    root_ids = set()
    for fname in os.listdir(input_dir):
        fpath = os.path.join(input_dir, fname)
        if os.path.isfile(fpath) and fname.endswith('.png'):
            root_ids.add(int(os.path.splitext(fname)[0]))
        elif os.path.isdir(fpath) and re.match(r'^\d+$', fname):
            root_ids.add(int(fname))

    index = 0
    for id in sorted(root_ids):
        delta_folder = os.path.join(input_dir, get_image_foldername(id))

        if os.path.isdir(delta_folder):
            # delta图组
            base_image = None
            base_index = 0
            for png_name in sorted(os.listdir(delta_folder)):
                if not png_name.endswith('.png'):
                    continue
                png_id = int(os.path.splitext(png_name)[0])
                png_path = os.path.join(delta_folder, png_name)

                if base_image is None:
                    base_index = index
                    base_image = CSystemImage(index)
                    base_image.load_from_png_as_csystem(png_path)

                    wrapper = CSystemImage(index)
                    wrapper.load_from_png_as_wrapper(png_path)
                    content = wrapper.write()
                    writer.write_entry(png_id, 'b', '0', content)
                    print(f"  封包图像 {png_id:06d} (基础)")
                    index += 1
                else:
                    delta = CSystemImage(base_index)
                    delta.load_from_png_as_csystem(png_path)
                    delta.convert_delta_to_full(base_image)  # 先转full再转delta
                    # 实际上应该从full转delta...这里简化: 直接用wrapper
                    wrapper = CSystemImage(index)
                    wrapper.load_from_png_as_wrapper(png_path)
                    content = wrapper.write()
                    writer.write_entry(png_id, 'b', '0', content)
                    print(f"  封包图像 {png_id:06d} (delta)")
                    index += 1
        else:
            png_path = os.path.join(input_dir, get_image_filename(id))
            if os.path.exists(png_path):
                img = CSystemImage(index)
                img.load_from_png_as_wrapper(png_path)
                content = img.write()
                writer.write_entry(id, 'b', '0', content)
                print(f"  封包图像 {id:06d}")
                index += 1


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description='Cyberworks C,system 引擎 DAT 存档工具 (Python版)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
用法示例:
  解包图像:
    python csystem_tool.py unpack Arc02.dat Arc05.dat Arc05a.dat images/

  封包图像:
    python csystem_tool.py pack 23 images/ Arc02_new.dat Arc05_new.dat

  解包脚本:
    python csystem_tool.py unpack Arc01.dat Arc04.dat scripts/

  封包脚本:
    python csystem_tool.py pack 23 scripts/ Arc01_new.dat Arc04_new.dat

  查看索引:
    python csystem_tool.py list Arc02.dat

  单独 b0->png:
    python csystem_tool.py b0topng file.b0 file.png

  单独 png->b0:
    python csystem_tool.py pngtob0 file.png file.b0
""")
    subparsers = parser.add_subparsers(dest='command')

    p = subparsers.add_parser('unpack', help='解包存档')
    p.add_argument('index_dat', help='索引DAT (如 Arc02.dat)')
    p.add_argument('content_dats', nargs='+', help='内容DAT + 输出目录 (最后一个参数为输出目录)')

    p = subparsers.add_parser('pack', help='封包存档')
    p.add_argument('version', type=int, help='存档版本号 (unpack时会显示)')
    p.add_argument('input_dir', help='输入目录')
    p.add_argument('index_dat', help='输出索引DAT')
    p.add_argument('content_dats', nargs='+', help='输出内容DAT')

    p = subparsers.add_parser('list', help='列出索引内容')
    p.add_argument('index_dat', help='索引DAT文件')

    p = subparsers.add_parser('b0topng', help='b0 数据转 PNG')
    p.add_argument('input', help='输入 .b0 文件')
    p.add_argument('output', nargs='?', help='输出 .png 文件')

    p = subparsers.add_parser('pngtob0', help='PNG 转 b0 数据')
    p.add_argument('input', help='输入 .png 文件')
    p.add_argument('output', nargs='?', help='输出 .b0 文件')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == 'unpack':
        # 最后一个参数是输出目录
        content_dats = args.content_dats[:-1]
        output_dir = args.content_dats[-1]
        unpack(args.index_dat, content_dats, output_dir)

    elif args.command == 'pack':
        pack(args.version, args.input_dir, args.index_dat, args.content_dats)

    elif args.command == 'list':
        with open(args.index_dat, 'rb') as f:
            index_data = bcd_decompress(f)
        stream = io.BytesIO(index_data)
        count = 0
        while stream.tell() < len(index_data):
            entry = ArchiveEntry()
            entry.read(stream)
            if count == 0:
                print(f"Archive version: {entry.version}")
            print(f"  {entry}")
            count += 1
        print(f"共 {count} 个条目")

    elif args.command == 'b0topng':
        out = args.output or os.path.splitext(args.input)[0] + '.png'
        with open(args.input, 'rb') as f:
            data = f.read()
        img = CSystemImage()
        img.read(data)
        img.save_as_png(out)
        print(f"-> {out}")

    elif args.command == 'pngtob0':
        out = args.output or os.path.splitext(args.input)[0] + '.b0'
        img = CSystemImage(0)
        img.load_from_png_as_wrapper(args.input)
        with open(out, 'wb') as f:
            f.write(img.write())
        print(f"-> {out}")


if __name__ == '__main__':
    main()
