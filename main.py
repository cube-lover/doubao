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


@register("doubao-draw", "", "豆包AI绘画插件，自动发送全部图片", "2.1")
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

    # ---------------- 改进的API调用方法：添加URL等待重试机制 ----------------
    async def _call_doubao_api_with_retry(self, desc, image_url=None, max_retries=2):
        """调用API，如果返回空URL则等待重试"""
        for attempt in range(max_retries + 1):
            urls = await self._call_doubao_api(desc, image_url)
            if urls:
                return urls
            if attempt < max_retries:
                wait_time = 5
                logger.warning(f"API返回空URL，等待{wait_time}秒后第{attempt + 1}次重试...")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"API调用{max_retries + 1}次均返回空URL")
        return []

    # ---------------- 修复：所有图片发送都使用10次重试 ----------------
    async def _send_first_image(self, event: AstrMessageEvent, url: str, max_retries=10):
        """只发送第一张图片，使用10次重试"""
        for attempt in range(max_retries):
            try:
                logger.info(f"第 {attempt + 1} 次尝试发送第一张图片")
                await event.send(event.chain_result([Plain("🖼️ 图片 1：\n"), Image.fromURL(url)]))
                logger.info("第一张图片发送成功")
                return True
            except Exception as e:
                logger.error(f"第 {attempt + 1} 次发送第一张图片失败: {e}")
                if attempt < max_retries - 1:
                    wait_time = 2
                    logger.warning(f"等待{wait_time}秒后继续重试...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"第一张图片发送达到最大重试次数{max_retries}，停止重试")
                    return False
        return False

    async def _send_remaining_images(self, event: AstrMessageEvent, urls: list, max_retries=10):
        """发送剩余的所有图片（使用已有链接），只重试发送，不涉及API请求"""
        if not urls or len(urls) <= 1:
            return True

        remaining_urls = urls[1:]  # 获取除第一张外的所有图片
        logger.info(f"开始发送剩余 {len(remaining_urls)} 张图片，最多重试{max_retries}次")

        for attempt in range(max_retries):
            try:
                logger.info(f"第 {attempt + 1} 次尝试发送剩余图片")

                # 构建剩余图片的消息链
                chain_components = [Plain("🖼️ 其他图片：\n")]
                for i, url in enumerate(remaining_urls, 2):
                    chain_components.extend([
                        Plain(f"\n图片 {i}：\n"),
                        Image.fromURL(url)
                    ])

                await event.send(event.chain_result(chain_components))
                logger.info(f"剩余 {len(remaining_urls)} 张图片发送成功")
                return True

            except Exception as e:
                logger.error(f"第 {attempt + 1} 次发送剩余图片失败: {e}")
                # 不管什么错误都继续重试，直到达到最大重试次数
                if attempt < max_retries - 1:
                    wait_time = 2
                    logger.warning(f"等待{wait_time}秒后继续重试...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"剩余图片发送达到最大重试次数{max_retries}，停止重试")
                    return False
        return False

    async def _send_image_urls_with_retry(self, event: AstrMessageEvent, urls, max_retries=10):
        """发送图片URL列表，使用已有链接分两个消息链发送"""
        if not urls:
            return False

        logger.info(f"开始发送 {len(urls)} 张图片，使用已有链接")

        # 1. 先发送第一张图片
        first_success = await self._send_first_image(event, urls[0], max_retries)

        # 2. 如果有剩余图片，发送剩余图片
        remaining_success = True
        if len(urls) > 1 and first_success:
            remaining_success = await self._send_remaining_images(event, urls, max_retries)

        # 只要第一张图片发送成功就返回True，剩余图片失败也不重新请求豆包
        return first_success

    # ---------------- 完整图片生成流程重试 ----------------
    async def _complete_image_generation_with_retry(self, event: AstrMessageEvent, desc, image_url=None, max_retries=2):
        """完整的图片生成流程，包含API调用和图片发送的重试"""
        for attempt in range(max_retries + 1):
            try:
                # 1. 调用API生成图片（只调用一次，获取链接）
                urls = await self._call_doubao_api_with_retry(desc, image_url)
                if not urls:
                    if attempt < max_retries:
                        logger.warning(f"图片生成失败，第 {attempt + 1} 次重试完整流程...")
                        await asyncio.sleep(3)
                        continue
                    else:
                        return False, "❌ 图片生成失败，可能是生成时间过长或网络问题"

                # 2. 使用已有的图片链接发送图片（不重新请求API）
                success = await self._send_image_urls_with_retry(event, urls)
                if success:
                    return True, None
                else:
                    # 注意：这里如果发送失败，会重新请求豆包生成新图片
                    if attempt < max_retries:
                        logger.warning(f"图片发送失败，第 {attempt + 1} 次重试完整流程...")
                        await asyncio.sleep(3)
                        continue
                    else:
                        return False, "❌ 图片发送失败，请稍后重试"

            except Exception as e:
                logger.error(f"完整流程异常: {e}")
                if attempt < max_retries:
                    logger.warning(f"流程异常，第 {attempt + 1} 次重试完整流程...")
                    await asyncio.sleep(3)
                    continue
                else:
                    return False, "❌ 图片处理异常，请稍后重试"

        return False, "❌ 图片生成失败，请稍后重试"

    # ---------------- 文生图（使用完整重试流程）---------------
    @filter.command("db")
    async def generate_image(self, event: AstrMessageEvent):
        msg_str = event.message_obj.message_str
        parts = msg_str.split(" ", 1)
        if len(parts) < 2:
            yield event.plain_result("使用方法：/db <描述词>\n例如：/db 一只可爱的猫")
            return

        desc = parts[1].strip()
        yield event.plain_result(
            f"🎨 正在生成图片...\n描述：{desc}\n风格：{self.current_style}\n比例：{self.current_ratio}\n模型：{self.current_model}"
        )

        success, error_msg = await self._complete_image_generation_with_retry(event, desc)
        if not success and error_msg:
            yield event.plain_result(error_msg)

    # ---------------- 图生图（使用完整重试流程）---------------
    @filter.command("jm")
    async def image_to_image(self, event: AstrMessageEvent):
        msg_str = event.message_obj.message_str
        parts = msg_str.split(" ", 2)
        if len(parts) < 2:
            yield event.plain_result("使用方法：/jm <描述词> + 图片/@用户/引用图片")
            return

        desc = parts[1].strip()
        if len(parts) == 3:
            third_part = parts[2].strip()
            if '@' in third_part or third_part.isdigit():
                pass
            else:
                desc = f"{desc} {third_part}"

        image_url = await self._get_image_url_from_event(event)

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

        success, error_msg = await self._complete_image_generation_with_retry(event, desc, image_url)
        if not success and error_msg:
            yield event.plain_result(error_msg)

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

    # ---------------- 手办化（使用完整重试流程）---------------
    @filter.command("手办化")
    async def transform_to_handmade(self, event: AstrMessageEvent):
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

        image_url = await self._get_image_url_from_event(event)
        if not image_url:
            m = re.search(r'\d+', event.message_obj.message_str)
            if m:
                qq = m.group(0)
                image_url = f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640"

        if not image_url:
            yield event.plain_result("❌ 没有找到图片，请发送图片或@用户或引用图片/GIF")
            return

        yield event.plain_result("🎨 正在生成手办化图片...")

        success, error_msg = await self._complete_image_generation_with_retry(event, prompt, image_url)
        if not success and error_msg:
            yield event.plain_result(error_msg)

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

    # ---------------- 调用API（添加详细错误日志）---------------
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

                    logger.info(f"API响应状态: {resp.status}")

                    if resp.status == 429:
                        logger.error(f"API返回429错误：密钥使用频率超限，响应内容: {text}")
                        return []
                    elif resp.status == 401:
                        logger.error(f"API返回401错误：认证失败，请检查API密钥，响应内容: {text}")
                        return []
                    elif resp.status == 403:
                        logger.error(f"API返回403错误：权限不足，响应内容: {text}")
                        return []
                    elif resp.status == 404:
                        logger.error(f"API返回404错误：接口不存在，响应内容: {text}")
                        return []
                    elif resp.status == 500:
                        logger.error(f"API返回500错误：服务器内部错误，响应内容: {text}")
                        return []
                    elif resp.status == 502:
                        logger.error(f"API返回502错误：网关错误，响应内容: {text}")
                        return []
                    elif resp.status == 503:
                        logger.error(f"API返回503错误：服务不可用，响应内容: {text}")
                        return []
                    elif resp.status != 200:
                        logger.error(f"API返回非200状态: {resp.status}, 响应内容: {text}")
                        return []

                    try:
                        data = json.loads(text)
                        logger.info(f"API响应JSON: {json.dumps(data, ensure_ascii=False)}")

                        if data.get('code') == 200 and 'image_url' in data:
                            urls = data['image_url']
                            if isinstance(urls, list):
                                logger.info(f"成功获取到 {len(urls)} 张图片")
                                return urls
                            elif isinstance(urls, str):
                                logger.info("成功获取到1张图片")
                                return [urls]
                            else:
                                logger.error(f"image_url格式异常: {type(urls)} - {urls}")
                                return []
                        else:
                            error_code = data.get('code', '未知')
                            error_msg = data.get('message', '未知错误')
                            logger.error(f"API业务错误: code={error_code}, message={error_msg}")
                            return []

                    except json.JSONDecodeError as e:
                        logger.error(f"JSON解析失败: {e}, 响应内容: {text}")
                        return []

        except aiohttp.ClientError as e:
            logger.error(f"网络请求失败: {e}")
            return []
        except asyncio.TimeoutError:
            logger.error("API请求超时")
            return []
        except Exception as e:
            logger.error(f"API调用未知错误: {e}")
            return []

    # ---------------- 辅助方法 ----------------
    def _extract_qq_from_at(self, text: str) -> str:
        m = re.search(r'\d+', text)
        return m.group(0) if m else None

    async def terminate(self):
        logger.info("豆包AI绘画插件已卸载")