# 汉化/修图实战工作流

本文档说明如何结合 GARbro 和 CSystem-Extractor 完成 C,system 引擎游戏的图像修改与封包。

## 为什么需要两个工具？

C,system 引擎的 CG 采用**多图层合成**机制 — 一张完整的 1280×720 画面由多个小图层在不同坐标叠加而成（背景层、人物层、表情层、前景层等）。单独解包某一张 `.b0` 图像只能拿到其中一个图层片段，不是完整画面。

| 工具 | 优势 | 局限 |
|------|------|------|
| **GARbro** | 自动图层合成，导出完整 1280×720 CG | 只能导出，不能封包回写 |
| **CSystem-Extractor** | 能解包也能封包，支持索引重建 | 图层合成需要脚本信息，单图层可能不完整 |

**结论：用 GARbro 导出完整图 → 修改 → 用 CSystem-Extractor 封包回写。**

---

## 完整工作流

### 第一步：用 GARbro 导出需要修改的完整 CG

1. 打开 GARbro，浏览到 `Arc05.dat`
2. 找到需要修改的图像（预览窗口显示完整合成效果）
3. 右键 → 导出为 PNG（得到完整的 1280×720 图像）
4. 记录文件名中的 **ID 编号**（如 `000914.b0` → ID 为 914）

> GARbro 导出的是合成后的完整画面，包含所有图层叠加效果。

### 第二步：修改图像

用 Photoshop / GIMP / 其他图像编辑器修改导出的 PNG：
- 翻译图中的日文文字
- 替换 UI 元素
- 调整画面内容
- ...

**保存为 PNG 格式，保持原始尺寸（通常 1280×720）。**

### 第三步：用 CSystem-Extractor 解包索引

```bash
# 先解包获取原始索引信息和所有资源
python csystem_tool.py unpack Arc02.dat Arc05.dat images/
```

记住输出的 `Archive version:` 数字（例如 23），封包时需要。

### 第四步：替换修改后的图像

把修改后的 PNG 放到解包目录中，**使用 type 'c'（标准包装）格式**替换原文件。

方法 A：手动转换单个文件

```bash
# 把修改后的 PNG 转为 b0 格式（标准包装 type 'c'，引擎直接支持）
python csystem_tool.py pngtob0 modified_914.png images/000914.b0
```

这会生成一个 type 'c' 的 `.b0` 文件，内部直接包裹 PNG 数据。引擎加载时通过 `OleLoadPicture` 或标准图像解码器读取，完全兼容。

方法 B：批量替换

```bash
# 把所有修改后的 PNG 放在 modified/ 目录下，文件名保持 000XXX.png 格式
# 然后批量转换
for f in modified/*.png; do
    id=$(basename "$f" .png)
    python csystem_tool.py pngtob0 "$f" "images/${id}.b0"
done
```

### 第五步：封包回写

```bash
# version 号从第三步的输出中获取
python csystem_tool.py pack 23 images/ Arc02_new.dat Arc05_new.dat
```

### 第六步：替换游戏文件

```bash
# 备份原文件
copy Arc02.dat Arc02.dat.bak
copy Arc05.dat Arc05.dat.bak

# 替换
copy Arc02_new.dat Arc02.dat
copy Arc05_new.dat Arc05.dat
```

启动游戏验证效果。

---

## 关于 type 'c' 替换的兼容性说明

原始游戏中的 CG 大多使用 type 'a'（自定义位图）+ type 'd'（delta 差分）存储，这是引擎的优化格式。用 type 'c'（标准包装 PNG）替换后：

**可以正常工作的情况：**
- 替换的图是**完整 CG**（base_index 指向自身，非 delta 图的基底）
- 替换后该 ID 对应的所有 delta 变体不再需要（或者一并替换）

**需要注意的情况：**
- 如果原始 ID 是其他 delta 图的 **base（基础图）**，替换为 type 'c' 后 delta 图的差分合并可能出问题
- 解决方案：把该基础图的所有 delta 变体也一起替换为独立的完整图

**推荐做法：**

对于一组 CG 变体（比如同一场景的不同表情），把**整组**都用 GARbro 导出完整图、修改、再全部用 type 'c' 替换回去。这样就不依赖 delta 差分机制了。

---

## 只修改部分图的精简流程

如果你只需要修改少量图片（比如标题画面、UI 按钮），不用解包全部资源：

```bash
# 1. 查看索引，找到目标 ID
python csystem_tool.py list Arc02.dat | findstr "b0"

# 2. 用 GARbro 导出目标图片，修改

# 3. 直接把修改后的 PNG 转为 b0
python csystem_tool.py pngtob0 title_modified.png title.b0

# 4. 解包 → 替换 → 封包
python csystem_tool.py unpack Arc02.dat Arc05.dat temp/
copy /Y title.b0 temp\000XXX.b0
python csystem_tool.py pack 23 temp/ Arc02.dat Arc05.dat
```

---

## 常见问题

**Q: 导出的图只有一部分（比如 1280×388 而不是 1280×720）？**

A: 这是正常的。C,system 引擎把一张完整 CG 拆成多个图层存储。用 GARbro 导出可以得到合成后的完整图。CSystem-Extractor 解包的是单个图层。

**Q: 封包后游戏显示异常？**

A: 检查是否有 delta 图依赖被替换的基础图。建议把同一组的所有变体都替换。

**Q: 封包后文件变大了？**

A: type 'c' 包裹的是完整 PNG 文件，比原始的 type 'a' 自定义格式 + delta 差分占用更多空间。这是正常的，不影响游戏运行。如果需要控制体积，可以在 `pngtob0` 之前用图像工具压缩 PNG。

**Q: 音频文件（.n0/.o0）怎么处理？**

A: 这些是混淆的 OGG 音频，目前工具按原样提取。如需替换音频，保持相同的混淆格式写回即可。
