from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import aiohttp
import json
import asyncio
import io
from PIL import Image as PILImage
import base64
import re
import os


@register("doubao-draw", "", "豆包AI绘画插件，自动发送全部图片", "2.0")
class DouBaoDraw(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.api_url = config.get("api_url")
        self.apikey = config.get("apikey")
        self.conversation_id = config.get("conversation_id")
        self.cookie = config.get("cookie")
        self.default_style = config.get("default_style")
        self.default_ratio = config.get("default_ratio")
        self.default_model = config.get("default_model")

        self.current_style = self.default_style
        self.current_ratio = self.default_ratio
        self.current_model = self.default_model

    # ---------------- 新增：带重试的消息发送方法 ----------------
    async def _send_message_with_retry(self, event: AstrMessageEvent, chain, max_retries=3):
        """发送消息并重试机制"""
        for attempt in range(max_retries):
            try:
                await event.send(chain)
                return True
            except Exception as e:
                error_msg = str(e)
                if "rich media transfer failed" in error_msg and attempt < max_retries - 1:
                    logger.warning(f"图片发送失败，第 {attempt + 1} 次重试...")
                    await asyncio.sleep(2)  # 等待2秒后重试
                    continue
                else:
                    logger.error(f"消息发送失败: {error_msg}")
                    raise e
        return False

    async def _send_image_chain_with_retry(self, event: AstrMessageEvent, chain_components, max_retries=3):
        """发送图片消息链并重试"""
        try:
            chain = event.chain_result(chain_components)
            await self._send_message_with_retry(event, chain, max_retries)
            return True
        except Exception as e:
            logger.error(f"图片消息链发送失败: {e}")
            # 发送纯文本替代
            try:
                text_chain = event.plain_result("❌ 图片发送失败，请稍后重试")
                await event.send(text_chain)
            except:
                pass
            return False

    # ---------------- 文生图 ----------------
    @filter.command("db")
    async def generate_image(self, event: AstrMessageEvent):
        parts = event.message_obj.message_str.split(" ", 1)
        if len(parts) < 2:
            yield event.plain_result("使用方法：/db <描述词>\n例如：/db 一只可爱的猫")
            return

        desc = parts[1].strip()
        yield event.plain_result(
            f"🎨 正在生成图片...\n描述：{desc}\n风格：{self.current_style}\n比例：{self.current_ratio}\n模型：{self.current_model}"
        )

        urls = await self._call_doubao_api(desc)
        if not urls:
            yield event.plain_result("❌ 图片生成失败")
            return

        # 发送第一张图片（带重试）
        if urls:
            success = await self._send_image_chain_with_retry(
                event,
                [Plain(f"🖼️ 图片 1：\n"), Image.fromURL(urls[0])]
            )
            if not success:
                return

        # 发送其他图片（带重试）
        if len(urls) > 1:
            chain_components = [Plain("🖼️ 其他图片：\n")]
            for i, url in enumerate(urls[1:], 2):
                chain_components.extend([
                    Plain(f"\n图片 {i}：\n"),
                    Image.fromURL(url)
                ])

            success = await self._send_image_chain_with_retry(event, chain_components)
            if not success:
                return

    # ---------------- 图生图 ----------------
    @filter.command("jm")
    async def image_to_image(self, event: AstrMessageEvent):
        msg_str = event.message_obj.message_str
        parts = msg_str.split(" ", 2)
        if len(parts) < 2:
            yield event.plain_result("使用方法：/jm <描述词> + 图片/@用户/引用图片")
            return

        desc = parts[1].strip()
        image_url = await self._get_image_url_from_event(event)

        # 如果没有找到图片，再尝试 @用户头像
        if not image_url and len(parts) >= 3:
            qq = self._extract_qq_from_at(parts[2])
            if qq:
                image_url = f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640"

        if not image_url:
            yield event.plain_result("❌ 没有找到图片，请发送图片或@用户或引用图片/GIF")
            return

        yield event.plain_result(
            f"🎨 正在基于图片生成...\n描述：{desc}\n风格：{self.current_style}\n比例：{self.current_ratio}\n模型：{self.current_model}"
        )

        urls = await self._call_doubao_api(desc, image_url)
        if not urls:
            yield event.plain_result("❌ 图片生成失败")
            return

        # 发送第一张图片（带重试）
        if urls:
            success = await self._send_image_chain_with_retry(
                event,
                [Plain(f"🖼️ 图片 1：\n"), Image.fromURL(urls[0])]
            )
            if not success:
                return

        # 发送其他图片（带重试）
        if len(urls) > 1:
            chain_components = [Plain("🖼️ 其他图片：\n")]
            for i, url in enumerate(urls[1:], 2):
                chain_components.extend([
                    Plain(f"\n图片 {i}：\n"),
                    Image.fromURL(url)
                ])

            success = await self._send_image_chain_with_retry(event, chain_components)
            if not success:
                return

    # ---------------- 设置命令 ----------------
    @filter.command("切换风格")
    async def switch_style(self, event: AstrMessageEvent, style: str):
        self.current_style = style
        yield event.plain_result(f"✅ 风格已切换到: {style}")

    @filter.command("切换比例")
    async def switch_ratio(self, event: AstrMessageEvent, ratio: str):
        self.current_ratio = ratio
        yield event.plain_result(f"✅ 比例已切换到: {ratio}")

    @filter.command("切换模型")
    async def switch_model(self, event: AstrMessageEvent, model: str):
        self.current_model = model
        yield event.plain_result(f"✅ 模型已切换到: {model}")

    @filter.command("db设置")
    async def show_settings(self, event: AstrMessageEvent):
        info = f"""🎨 当前绘画设置：
风格：{self.current_style}
比例：{self.current_ratio}
模型：{self.current_model}

指令：
/db <描述词> - 文生图
/jm <描述词> - 图生图（支持图片/@用户/引用图片/GIF）
/切换风格 <风格>
/切换比例 <比例>
/切换模型 <模型>"""
        yield event.plain_result(info)

    # ---------------- 手办化 ----------------
    @filter.command("手办化")
    async def transform_to_handmade(self, event: AstrMessageEvent):
        """
        手办化命令，支持直接图片/引用/@用户/GIF
        """
        prompt = ("Please accurately transform the main subject in this photo into a realistic, masterpiece-like "
                  "1/7 scale PVC statue. The statue should be placed on a round plastic base and occupy a small portion "
                  "of the scene, so that the tabletop, surrounding objects, and background are clearly visible and well "
                  "balanced. Behind the statue, place a box with a large, clear transparent front window showing the main "
                  "artwork, product name, brand logo, barcode, and a small specification or authenticity verification panel. "
                  "A small price tag sticker should be attached to a corner of the box. A computer monitor is placed at the "
                  "back, displaying the ZBrush modeling process of the statue. The viewpoint should be pulled back enough to "
                  "include the entire statue, packaging box, monitor, and most of the tabletop in the frame, without cutting "
                  "off any important elements or the edges of the desk. The tabletop should contain clearly visible, photorealistic "
                  "objects such as books, notebooks, pens, smartphones, coffee cups, mugs, small handcrafts, miniature pets, and "
                  "small collectible figures of well-known cartoon and animated characters, arranged naturally and randomly. Emphasize "
                  "all objects using weighting so they appear clearly in the generated image: ((desk with books, smartphone, coffee cup, "
                  "pens, small handcrafts, miniature pets, cartoon character figures)). The miniature pets and cartoon character figures "
                  "on the desk should have colors matching or harmonizing with the main statue's primary colors, not plain white, and each "
                  "miniature pet or cartoon figure should be approximately one-third the height of the main statue, arranged naturally so "
                  "they interact or visually connect with the main character. The smartphone on the desk should have a very clearly visible "
                  "wallpaper showing the main statue, large enough to be immediately recognizable, and also display standard apps and interface "
                  "as on a real phone. Ensure the entire scene has natural and dynamic lighting with realistic shadows and reflections on all objects, "
                  "emphasizing depth and three-dimensionality, and is rendered in ultra-high definition with maximum clarity and photorealistic details. "
                  "This ensures the environment is fully visible and immersive while the statue remains the focal point. Special emphasis on the statue's "
                  "face and texture: The facial features must be perfectly restored, highly detailed, and ultra-HD, capturing expressions and details accurately "
                  "from the original photo. The PVC material of the statue must appear realistic and tangible, with natural texture, subtle reflections, and carefully "
                  "rendered light and shadow to enhance volume and realism. The figure should have a small amount of shadow and lighting variation on the body and face "
                  "to further enhance depth and three-dimensionality. The statue, packaging, and all surrounding items must be photorealistic, with proper lighting, "
                  "reflections, and 3D dimensionality. No outline lines should be present, and the statue must not appear flat. Guidelines: Repair any missing parts carefully; "
                  "no poorly executed elements allowed. Human figures (if applicable) must have natural body parts, coordinated movements, and correct proportions. "
                  "If the original photo is not full-body, supplement the statue to make it a full-body version. Human expressions and movements must match the photo exactly. "
                  "The figure's head should not be too large, legs not too short, and overall figure not stunted (ignore for chibi-style designs). For animals, reduce fur realism "
                  "and detail so it looks more like a statue than a real creature. Pay attention to perspective: near objects larger, distant objects smaller. Additional emphasis: "
                  "Ensure all objects on the desk, the statue, the box, and the monitor are fully visible, the composition is balanced, and the scene feels natural and lived-in. "
                  "The environment should be immersive, while the hand-painted statue face is ultra-detailed, realistic, and has subtle shadows for depth. Render everything in the "
                  "highest resolution, ultra-clear, and with the finest photorealistic quality possible, with enhanced realistic lighting, shadows, and reflections.")

        # 获取图片
        image_url = await self._get_image_url_from_event(event)
        if not image_url:
            # 尝试@用户头像
            m = re.search(r'\d+', event.message_obj.message_str)
            if m:
                qq = m.group(0)
                image_url = f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640"

        if not image_url:
            yield event.plain_result("❌ 没有找到图片，请发送图片或@用户或引用图片/GIF")
            return

        yield event.plain_result("🎨 正在生成手办化图片...")
        urls = await self._call_doubao_api(prompt, image_url)
        if not urls:
            yield event.plain_result("❌ 手办化生成失败")
            return

        # 发送第一张图片（带重试）
        if urls:
            success = await self._send_image_chain_with_retry(
                event,
                [Plain(f"🖼️ 图片 1：\n"), Image.fromURL(urls[0])]
            )
            if not success:
                return

        # 发送其他图片（带重试）
        if len(urls) > 1:
            chain_components = [Plain("🖼️ 其他图片：\n")]
            for i, url in enumerate(urls[1:], 2):
                chain_components.extend([
                    Plain(f"\n图片 {i}：\n"),
                    Image.fromURL(url)
                ])

            success = await self._send_image_chain_with_retry(event, chain_components)
            if not success:
                return

    # ---------------- 核心修复：图片获取 ----------------
    async def _get_image_url_from_event(self, event: AstrMessageEvent) -> str:
        """统一处理直接图片/引用图片/GIF"""
        chain = event.message_obj.message
        for seg in chain:
            if isinstance(seg, Image):
                return await self._process_image_segment(seg)
            elif isinstance(seg, Reply):
                for reply_seg in getattr(seg, 'chain', []):
                    if isinstance(reply_seg, Image):
                        return await self._process_image_segment(reply_seg)
        return ""

    async def _process_image_segment(self, image_seg: Image) -> str:
        if getattr(image_seg, 'url', None):
            if image_seg.url.lower().endswith('.gif'):
                return await self._gif_to_base64(image_seg.url)
            return image_seg.url
        elif getattr(image_seg, 'file', None):
            return await self._file_to_base64(image_seg.file)
        return ""

    async def _gif_to_base64(self, gif_url: str) -> str:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(gif_url) as resp:
                    if resp.status == 200:
                        gif_bytes = await resp.read()
                        img = PILImage.open(io.BytesIO(gif_bytes))
                        img.seek(0)
                        first_frame = img.convert("RGBA")
                        out_io = io.BytesIO()
                        first_frame.save(out_io, format="PNG")
                        return f"data:image/png;base64,{base64.b64encode(out_io.getvalue()).decode()}"
        except:
            pass
        return gif_url

    async def _file_to_base64(self, file_path: str) -> str:
        try:
            if os.path.exists(file_path):
                with open(file_path, 'rb') as f:
                    file_bytes = f.read()
                ext = os.path.splitext(file_path)[1].lower()
                mime_type = f"image/{ext[1:]}" if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp'] else "image/jpeg"
                return f"data:{mime_type};base64,{base64.b64encode(file_bytes).decode()}"
        except:
            pass
        return ""

    # ---------------- 调用API ----------------
    async def _call_doubao_api(self, desc, image_url=None):
        params = {
            'description': desc,
            'type': self.current_style,
            'ratio': self.current_ratio,
            'model': self.current_model,
            'conversation_id': self.conversation_id,
            'Cookie': self.cookie,
            'apikey': self.apikey
        }
        if image_url:
            params['url'] = image_url

        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.api_url, params=params, headers=headers, timeout=120) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        return []
                    data = json.loads(text)
                    if data.get('code') == 200 and 'image_url' in data:
                        urls = data['image_url']
                        if isinstance(urls, list):
                            return urls
                        elif isinstance(urls, str):
                            return [urls]
        except:
            pass
        return []

    # ---------------- 辅助方法 ----------------
    def _extract_qq_from_at(self, text: str) -> str:
        m = re.search(r'\d+', text)
        return m.group(0) if m else None

    async def terminate(self):
        logger.info("豆包AI绘画插件已卸载")