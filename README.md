# 🧠 DouBaoDraw 豆包AI绘画插件

> 版本：2.0  
> 作者：Jimeng  
> 适用平台：AstrBot  
> 功能类型：AI绘画 / 图片生成 / 图生图 / 手办化转换
> 群聊:689427254,有问题问群管(群主是个小男娘哦)

---

## 📘 插件简介

**DouBaoDraw** 是一个基于豆包AI绘画接口开发的 AstrBot 插件。  
它支持从文字或图片生成高质量图像，并可自动处理消息引用、GIF、以及 QQ 用户头像。  
新增的「手办化」命令可将任意图片或头像生成高拟真 1/7 比例 **PVC 手办效果图**。

---

## ✨ 功能列表

| 功能 | 说明 |
|------|------|
| `/db <描述词>` | 文生图：根据文字描述生成图片 |
| `/jm <描述词> + 图片/@用户/引用图片` | 图生图：根据图片与文字生成新图像 |
| `手办化` | 将任意图片、GIF 或头像生成高拟真手办场景 |
| 自动识别 | 引用消息中的图片、GIF、@用户头像 |
| 多图输出 | 自动发送生成的全部图片 |
| 错误提示 | 显示生成失败原因或异常 |

---

## 💡 使用示例

### 文生图
```
/db 一只戴草帽的橘猫坐在沙滩上
```

### 图生图（引用或@用户）
```
/jm 魔幻风格 @十二
/jm 梦幻森林 （引用一张图片）
```

### 手办化
```
手办化 @小明
手办化 （引用一张图片）
手办化 + 动态GIF
```

生成效果示例👇  
（自动生成多张图，每张独立发送）

---

## ⚙️ 配置项说明

`config.json` 示例：

```json
{
  "api_url": "https://example.com/api/doubao",
  "apikey": "your_api_key",
  "conversation_id": "doubao-001",
  "cookie": "session_cookie_here",
  "default_style": "realistic",
  "default_ratio": "1:1",
  "default_model": "doubao-v2"
}
```

| 参数 | 说明 |
|------|------|
| `api_url` | 豆包AI绘画接口地址 |
| `apikey` | 上叶的秋官网获取免费密钥 |
| `conversation_id` | 在监听到的对话js文件中寻找,在响应里面 |
| `cookie` | 豆包认证 Cookie |
| `default_style` | 默认绘画风格 |
| `default_ratio` | 图片宽高比 |
| `default_model` | 模型名称 |

---

## 🧩 插件文件结构

```
data/
└── plugins/
    └── doubao-draw/
        ├── main.py              # 主程序
        ├── config.json          # 配置文件
        └── README.md            # 插件说明
```

---

## 🧠 技术说明

- 异步架构：使用 `aiohttp` 实现异步请求，防止阻塞。  
- 图片处理：自动识别 `Image` / `Reply` / `GIF` / `@用户头像`。  
- 消息链支持：统一返回 AstrBot `chain_result` 格式。  
- 安全性：支持异常捕获与错误提示，防止因单次失败导致插件崩溃。  
- GIF处理：自动提取第一帧并转为 PNG 后发送。  

---

## 🪄 手办化提示词（英文）

> 插件内置完整高质量提示词，描述了 1/7 PVC 手办的构图、光照、材质、场景细节、配色及环境要求，确保生成画面立体、真实、自然。

如需修改，可在 `main.py` 中搜索：
```python
prompt = "Please accurately transform..."
```
自行替换或调整描述。

---

## 🔧 常见问题

| 问题 | 解决方案 |
|------|------------|
| 插件载入失败：IndexError | 请确认文件中存在 `@register` 装饰的类定义 |
| 提示 “object MessageEventResult can’t be used in await expression” | 确保 yield 用于发送消息链，而不是 `await` |
| 图片未识别 | 确保发送的消息中含有图片或引用包含图片的消息 |
| 手办化生成失败 | 检查接口返回值、apikey 和网络连接 |

---

## 🧩 卸载插件

在控制台输入：
```
/卸载插件 doubao-draw
```
或删除文件夹：
```
data/plugins/doubao-draw/
```

---

## 🧾 更新日志

### v2.0
- ✅ 新增 `手办化` 指令  
- ✅ 支持 GIF、引用消息、@用户头像  
- ✅ 优化消息链逻辑与图片提取流程  
- ✅ 增强异常处理与错误提示  
- ✅ 支持多图输出  

---

## 📜 许可证

本插件仅供学习与研究使用，禁止用于商业用途。  
接口来源及数据内容归豆包AI及相关平台所有。

---

**© 2025 Jimeng | Powered by AstrBot**

