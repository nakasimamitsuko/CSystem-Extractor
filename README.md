# CSystem-Extractor

Cyberworks **C,system** 视觉小说引擎的 DAT 存档解包/封包 + 图像转换工具（Python 实现）。

基于对 `tumahaha.exe` 的 Ghidra 逆向分析，并参考 [CSystemTools](https://github.com/arcusmaximus/CSystemTools)（C#）验证格式。

## 功能

- 解包/封包索引 DAT（Arc01/02/03/07/09）和内容 DAT（Arc04/05/06/08/10）
- 自动解压 LZSS 压缩的资源（类型 b/c/e/n/o/p）
- 图像自动转换：b0 ⟷ PNG（支持三种子格式：标准包装 / 自定义位图 / delta 差分）
- 处理 delta 图像的自动合并（基于 mask 位图的差分还原）
- 纯 Python 实现，仅依赖 Pillow

## 快速开始

```bash
pip install Pillow

# 解包图像（Arc02 = 图像索引，Arc05 系列 = 图像内容）
python csystem_tool.py unpack Arc02.dat Arc05.dat Arc05a.dat Arc05b.dat images/

# 封包图像（版本号从 unpack 输出获取）
python csystem_tool.py pack 23 images/ Arc02_new.dat Arc05_new.dat

# 解包脚本
python csystem_tool.py unpack Arc01.dat Arc04.dat scripts/

# 查看索引内容
python csystem_tool.py list Arc02.dat

# 单独转换
python csystem_tool.py b0topng file.b0 output.png
python csystem_tool.py pngtob0 input.png output.b0
```

## 文件对应关系

| 索引文件 | 内容文件 | 资源类型 |
|---------|---------|---------|
| Arc00.dat | — | 全局配置（窗口标题、字体大小等） |
| Arc01.dat | Arc04.dat | 剧本脚本 (.a0) |
| Arc02.dat | Arc05.dat, Arc05a.dat, Arc05b.dat ... | 图像 (.b0) |
| Arc03.dat | Arc06.dat | 音频（混淆的 OGG） |
| Arc07.dat | Arc08.dat, Arc08a.dat | 未知资源 |
| Arc09.dat | Arc10.dat | 未知资源 |

---

## 逆向工程分析

以下记录从 `tumahaha.exe`（32位 PE，MFC 应用）出发，通过 Ghidra 反编译逐步还原 C,system 引擎存档格式的完整过程。

### 第一步：定位入口 — DAT 文件名字符串

在 Ghidra 中搜索宽字符串 `Arc00.dat`，定位到函数 `FUN_004247a0`（存档初始化函数）。该函数依次加载所有 Arc 文件：

```c
// FUN_004247a0 — 存档加载入口
FUN_0041a250(local_82c, L"\\Arc00.dat", 10);  // 拼接路径
iVar1 = FUN_004263f0(this, local_108c, pppwVar3);  // 加载 Arc00（特殊处理）

FUN_0041a250(local_82c, L"\\Arc01.dat", 10);
iVar1 = FUN_00426170(&local_20dc, pppwVar3);  // 加载 Arc01（通用索引）

// Arc04 通过虚函数调用 CFile::Open 直接打开（内容文件，无头部）
(**(code **)(*(int *)((int)this + 0x28) + 0x24))(pppwVar3, 0x2010, 0);

FUN_0041a250(local_82c, L"\\Arc05.dat", 10);
(**(code **)(*(int *)((int)this + 0x3c) + 0x24))(pppwVar3, 0x2010, 0);
// ... Arc05a, Arc05b, Arc05c, Arc05d, Arc03, Arc06, Arc07, Arc08, Arc09, Arc10
```

**关键发现**：Arc01/02/03/07/09 走 `FUN_00426170`（有 BCD 头部 + LZSS 解压），而 Arc04/05/06/08/10 通过 `CFile::Open` 直接打开（裸数据，无头部）。

### 第二步：BCD 头部编码 — XOR 0x7F 的十进制数

`FUN_00426170` 的开头是 BCD 解码逻辑，每个字节 XOR 0x7F 得到一位十进制数字：

```c
// FUN_00426170 — 索引文件加载
_Memory = malloc(file_length);
CFile::Read(&file, _Memory, file_length);

// 前 8 字节 → 解压后大小（XOR 0x7F 编码的 8 位十进制数）
if (*_Memory == 0xFF) { iVar2 = 0; }
else { iVar2 = (char)(*_Memory ^ 0x7F) * 10000000; }
if (_Memory[1] != 0xFF) { iVar2 += (char)(_Memory[1] ^ 0x7F) * 1000000; }
if (_Memory[2] != 0xFF) { iVar2 += (char)(_Memory[2] ^ 0x7F) * 100000; }
// ... 逐位累加到个位
// 后 8 字节 → 压缩数据大小（同样编码）
```

`0xFF` 表示该位为 0。例如 `FF FF 7D 78 FF 76 7E 7E` 解码为：`0 0 4 1 0 9 1 1` → 410911。

### 第三步：LZSS 解压算法

`FUN_00427a20` 是 LZSS 解压函数，参数为环形缓冲区所在的类实例：

```c
// FUN_00427a20 — LZSS 解压
// 初始化: 4096 字节环形缓冲区，全部填充 0x00，初始位置 0xFEE
for (i = 0x3FB; i != 0; i--) { *puVar10 = 0; puVar10++; }
uVar9 = 0xFEE;  // 初始写入位置 = WindowSize - MaxMatchLength

while (true) {
    uVar7 >>= 1;
    if ((uVar7 & 0x100) == 0) {        // 需要新的 flag 字节
        uVar7 = src[src_pos++] | 0xFF00;
    }
    if (uVar7 & 1) {                    // bit=1: literal 直接拷贝
        byte b = src[src_pos++];
        output[out_pos++] = b;
        ring[ring_pos] = b;
        ring_pos = (ring_pos + 1) & 0xFFF;
    } else {                            // bit=0: reference 回溯引用
        byte low = src[src_pos];
        byte high = src[src_pos + 1];
        src_pos += 2;
        int offset = low | ((high & 0xF0) << 4);   // 12-bit offset
        int length = (high & 0x0F) + 3;             // 4-bit length + 3
        for (i = 0; i < length; i++) {
            byte b = ring[(offset + i) & 0xFFF];
            output[out_pos++] = b;
            ring[ring_pos] = b;
            ring_pos = (ring_pos + 1) & 0xFFF;
        }
    }
}
```

算法参数：窗口大小 4096 (0x1000)，最大匹配长度 18 (0x0F + 3)，匹配阈值 2，初始填充 0x00。

### 第四步：索引条目结构

解压后的数据由 `FUN_00420390` 逐条解析。通过分析循环步长 `0x1B` (27 字节) 和各字段的 `memmove` 偏移，还原出条目结构：

```c
// FUN_00420390 — 索引条目解析
// 每条 0x1B 字节 (version <= 0x16) 或 0x1C 字节 (version > 0x16)
do {
    memmove(&local_86c, data + offset + 0x00, 4);  // Version (int32)
    memmove(&local_220, data + offset + 0x04, 4);  // Id (int32)
    memmove(local_224,  data + offset + 0x08, 4);  // UncompressedSize (int32)
    memmove(local_228,  data + offset + 0x0C, 4);  // CompressedSize (int32)
    memmove(local_22c,  data + offset + 0x10, 4);  // Offset (int32)
    memmove(local_42c,  data + offset + 0x14, 1);  // Type (byte)
    memmove(local_21c,  data + offset + 0x15, 1);  // SubType (byte)
    memmove(local_1c,   data + offset + 0x16, 4);  // Reserved (int32)
    memmove(local_18,   data + offset + 0x1A, 1);  // ContentArchiveIndex (byte)
    offset += 0x1B;

    // 根据 Type 分发到不同的资源管理器
    if (wcsstr(local_42c, L"a"))      { slot = this + 0x11C; }
    else if (wcsstr(local_42c, L"b")) { /* 图像 → 多个子槽 */ }
    else if (wcsstr(local_42c, L"c")) { ... }
    // ... 完整的 a-z + 0 类型分发表
} while (offset < total_size);
```

### 第五步：资源读取 — 压缩 vs 非压缩

`FUN_00420b80` 是运行时资源读取函数。从 0x834 大小的结构体中读取元数据，然后对内容文件做 Seek + Read：

```c
// FUN_00420b80 — 从内容 DAT 读取资源
EnterCriticalSection(&this->cs);
int decomp_size = *(int*)(entry + 0x20C);   // UncompressedSize
this->output_buf = malloc(decomp_size + 1);

size_t comp_size = *(int*)(entry + 0x208);   // CompressedSize
if (param_3 == 0) {  // 不需要解压
    CFile::Seek(content_file, *(int*)(entry + 0x204), 0, 0);  // Offset
    CFile::Read(content_file, this->output_buf, comp_size);
} else {             // 需要 LZSS 解压
    this->compress_buf = malloc(comp_size);
    CFile::Seek(content_file, *(int*)(entry + 0x204), 0, 0);
    CFile::Read(content_file, this->compress_buf, comp_size);
    FUN_00427a20(this, decomp_size, comp_size);  // LZSS 解压
}
```

通过对照 `ArchiveWriter` 中的条件 `type == 'b' || type == 'c' || type == 'e' || type == 'n' || type == 'o' || type == 'p'`，确认了哪些类型会被压缩。

### 第六步：图像格式 — b0 的三种子格式

`FUN_00485f20` 是图像加载入口。根据首字节判断子格式：

```c
// FUN_00485f20 — 图像加载
if (*param_1 == 'a' || *param_1 == 'd') {
    // 自定义位图格式 (CSystem native)
    // 'a' = 完整图 (full + alpha)
    // 'd' = delta 差分图 (mask + 变化部分)
    // 参数用 AttrValue 编码 (base=0xE9, end=0xEF, zero=0xFB)
    goto handle_csystem_image;
}
else if (*param_1 == 'b') {
    // 这个分支实际是 OLE 图片（旧版兼容），当前版本游戏主要用 a/c/d
    size = CONCAT31(param_1[1], param_1[2], param_1[3], param_1[4]);  // BE uint32
    GlobalAlloc(0x40, size);
    memcpy(dst, param_1 + 5, size);
    CreateStreamOnHGlobal(hGlobal, 1, &pStream);
    OleLoadPicture(pStream, size, 0, IID_IPicture, &pPicture);
    // 渲染到 24位 DIBSection
}
else if (*param_1 == 'c') {
    // 标准图像包装
    size = (param_1[1] << 24) | (param_1[2] << 16) | (param_1[3] << 8) | param_1[4];
    memcpy(buffer, param_1 + 5, size);  // 内部是标准 BMP/JPG/PNG
}
```

通过 CSystemTools 源码验证：

- **'c' (标准包装)**：`1B type + 4B big-endian size + raw image data`。内部可以是任何 OLE 支持的格式（BMP/JPEG/PNG/GIF）。封包时直接把 PNG 包进去即可。
- **'a' (自定义完整图)**：使用 AttrValue 编码的参数头 + BGR color 数据 + 可选 alpha 通道。行宽对齐规则为 `width * 3 + (width & 3)`。
- **'d' (delta 差分图)**：在 'a' 的基础上增加了 mask 位图。mask 中 bit=1 的像素取 delta 数据，bit=0 的像素取基础图数据。

AttrValue 编码是一种自定义的变长整数编码：

```
值 = (hundreds × 0xE9 + tens) × 0xE9 + units
其中: units = 首字节 (0xFB 表示 0)
      tens  = 中间字节 (非 0xE9 且非 0xEF 的字节, 0xFB 表示 0)
      hundreds = 0xE9 出现的次数
      0xEF = 终止符
```

### 第七步：Arc00 配置文件

`FUN_00425590` 解析 Arc00.dat（配置文件，不走 BCD+LZSS）。通过逆向定位到的配置键名：

```c
// UTF-16LE 配置键
local_18c = { 'W', ':', 0, 0,    // 窗口宽度
              'H', ':', 0, 0,    // 窗口高度
              'T', ':', 0, 0,    // 标题
              'C', ':', 0, 0,    // 字符集
              'F', ':', 0, 0,    // 字体大小
              'N', ':', 0, 0,    // 名称
              'X', ':', 0, 0,    // 字间距
              'Y', ':', 0, 0 };  // 行间距
// 'AT:', 'AL:', 'AB:', 'AR:' — 消息窗口的 top/left/bottom/right
// 'M:' — 最大行数
// 0xC9 — 字段分隔符
```

### 逆向总结

| 函数地址 | 功能 | 对应实现 |
|---------|------|---------|
| `FUN_004247a0` | 存档初始化，加载所有 Arc 文件 | `ArchiveReader.__init__` |
| `FUN_00426170` | 索引文件解压（BCD头部 + LZSS） | `bcd_decompress` |
| `FUN_004263f0` | Arc00 特殊加载（多段数据） | 配置解析 |
| `FUN_00427a20` | LZSS 解压 | `lzss_decompress` |
| `FUN_00420390` | 索引条目解析 + 类型分发 | `ArchiveEntry.read` |
| `FUN_00420b80` | 运行时资源读取（Seek + Read + 可选解压） | `ArchiveReader.get_entry_content` |
| `FUN_00425590` | Arc00 配置解析 | `CSystemConfig` |
| `FUN_00485f20` | 图像加载（a/b/c/d 分支） | `CSystemImage.read` |
| `FUN_00482420` | 指令流解析（T/M/N/A 指令） | 脚本执行器 |

---

## 格式规格

### 索引文件 (Arc01/02/03/07/09.dat)

```
┌─────────────────────────────────────────────┐
│ BCD Header (16 bytes)                       │
│   [0:8]  XOR 0x7F → decompressed_size      │
│   [8:16] XOR 0x7F → compressed_size        │
├─────────────────────────────────────────────┤
│ LZSS Compressed Data                        │
│   解压后为 ArchiveEntry[] 连续排列           │
└─────────────────────────────────────────────┘
```

### ArchiveEntry (27 或 28 字节, little-endian)

```
┌──────┬──────┬──────────────────────┐
│ +00  │ int32│ Version              │
│ +04  │ int32│ Id                   │
│ +08  │ int32│ UncompressedSize     │
│ +0C  │ int32│ CompressedSize       │
│ +10  │ int32│ Offset               │
│ +14  │ byte │ Type                 │
│ +15  │ byte │ SubType              │
│ +16  │ int32│ Reserved (0xFFFFFFFF)│
│ +1A  │ byte │ ContentArchiveIndex  │ ← 仅 Version > 0x16
└──────┴──────┴──────────────────────┘
```

### 内容文件 (Arc04/05/06/08/10.dat)

无头部，纯数据。每个资源通过索引的 `Offset` 定位、`CompressedSize` 读取、`UncompressedSize` 解压。

### 图像子格式

```
Type 'c' (标准包装):
  [0]      byte  = 'c' (0x63)
  [1:5]    uint32 = data_size (big-endian)
  [5:5+N]  bytes  = 标准图像数据 (BMP/JPEG/PNG)

Type 'a' (CSystem 完整图):
  [0]      byte  = 'a' (0x61)
  [1]      byte  = 0xEF (end marker)
  [2..]    AttrValue × 19 个参数字段
           (base_index, 0, 0, width, 0, height, 0, 0, 0,
            alpha_size, 0, color_size, 0, flags, 0, 0, 0,
            mask_size, 0, 0)
  [...]    mask_data  (mask_size bytes, 如果有)
  [...]    alpha_data (alpha_size bytes, 如果有)
  [...]    color_data (color_size bytes, BGR格式)

Type 'd' (delta 差分图):
  同 'a'，但首字节为 'd' (0x64)，且包含 mask 数据
  mask 中每个 bit 对应一个像素: 1=取 delta, 0=取基础图
```

## 致谢

- [CSystemTools](https://github.com/arcusmaximus/CSystemTools) — arcusmaximus 的 C# 实现，用于验证和补全格式细节
- [GARbro](https://github.com/morkt/GARbro) — morkt 的 Cyberworks/TinkerBell 格式支持
- [Ghidra](https://ghidra-sre.org/) — NSA 的逆向工程框架

## License

MIT
