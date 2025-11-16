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
import time


@register("doubao-draw", "", "豆包AI绘画插件，支持Cookie轮询和黑名单", "3.0")
class DouBaoDraw(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.api_url = config.get("api_url")
        self.apikey = config.get("apikey")
        self.default_style = config.get("default_style")
        self.default_ratio = config.get("default_ratio")
        self.default_model = config.get("default_model")

        self.current_style = self.default_style
        self.current_ratio = self.default_ratio
        self.current_model = self.default_model

        # 存储自定义提示词和Cookie对
        self.prompt_map: dict = {}
        self.cookie_pairs: list = []
        self.current_cookie_index = 0
        self._last_rate_limit = False

        # Cookie黑名单和失败计数
        self._cookie_blacklist = set()  # 被限制的Cookie conversation_id
        self._cookie_fail_count = {}  # 每个Cookie的失败次数
        self._max_failures = 10  # 最大失败次数

        # 加载配置
        self._load_prompt_map(config)
        self._load_cookie_pairs(config)

    def _load_prompt_map(self, config: dict):
        """加载自定义提示词"""
        logger.info("正在加载自定义提示词...")
        self.prompt_map.clear()
        prompt_list = config.get("prompt_list", [])
        for item in prompt_list:
            try:
                if ":" in item:
                    key, value = item.split(":", 1)
                    self.prompt_map[key.strip()] = value.strip()
                    logger.info(f"加载提示词: {key} -> {value[:50]}...")
            except ValueError:
                logger.warning(f"跳过格式错误的提示词: {item}")
        logger.info(f"共加载了 {len(self.prompt_map)} 个自定义提示词")

    def _load_cookie_pairs(self, config: dict):
        """加载Cookie对配置 - 修复版本：不跳过黑名单中的Cookie"""
        logger.info("正在加载Cookie对...")
        self.cookie_pairs.clear()

        # 先尝试从cookie_list获取多组Cookie
        cookie_list = config.get("cookie_list", [])
        logger.info(f"从配置读取到cookie_list: {cookie_list}")

        for item in cookie_list:
            try:
                if ":" in item:
                    conversation_id, cookie = item.split(":", 1)
                    conv_id = conversation_id.strip()
                    # 关键修改：不再跳过黑名单中的Cookie，只是加载但不使用
                    self.cookie_pairs.append({
                        'conversation_id': conv_id,
                        'cookie': cookie.strip(),
                        'in_blacklist': conv_id in self._cookie_blacklist  # 标记是否在黑名单中
                    })
                    logger.info(f"加载Cookie对: {conv_id} (黑名单: {conv_id in self._cookie_blacklist})")
            except ValueError:
                logger.warning(f"跳过格式错误的Cookie对: {item}")

    def _get_available_cookie_pairs(self):
        """获取可用的Cookie对（不在黑名单中的）"""
        return [pair for pair in self.cookie_pairs if not pair.get('in_blacklist', False)]

    def _get_current_cookie_pair(self):
        """获取当前使用的Cookie对"""
        available_pairs = self._get_available_cookie_pairs()
        if not available_pairs:
            return None
        # 确保当前索引在可用范围内
        if self.current_cookie_index >= len(available_pairs):
            self.current_cookie_index = 0
        return available_pairs[self.current_cookie_index]

    def _switch_to_next_cookie(self):
        """切换到下一组Cookie对"""
        available_pairs = self._get_available_cookie_pairs()
        if not available_pairs:
            return False

        old_index = self.current_cookie_index
        # 寻找下一个可用的Cookie
        self.current_cookie_index = (self.current_cookie_index + 1) % len(available_pairs)
        logger.info(f"从Cookie对 {old_index} 切换到 {self.current_cookie_index}")
        return True

    def _add_to_blacklist(self, conversation_id):
        """将Cookie添加到黑名单"""
        if conversation_id not in self._cookie_blacklist:
            self._cookie_blacklist.add(conversation_id)
            logger.warning(f"已将Cookie {conversation_id} 添加到黑名单")

            # 更新cookie_pairs中的标记
            for pair in self.cookie_pairs:
                if pair['conversation_id'] == conversation_id:
                    pair['in_blacklist'] = True
                    break

            # 如果删除的是当前使用的cookie，切换到下一个
            current_pair = self._get_current_cookie_pair()
            if not current_pair:
                self.current_cookie_index = 0
                logger.info(f"当前Cookie被加入黑名单，自动切换到索引 {self.current_cookie_index}")

    def _remove_from_blacklist(self, conversation_id):
        """从黑名单中移除Cookie"""
        if conversation_id in self._cookie_blacklist:
            self._cookie_blacklist.remove(conversation_id)
            # 更新cookie_pairs中的标记
            for pair in self.cookie_pairs:
                if pair['conversation_id'] == conversation_id:
                    pair['in_blacklist'] = False
                    break
            logger.info(f"已将Cookie {conversation_id} 从黑名单移除")

    def _record_failure(self, conversation_id):
        """记录Cookie失败次数"""
        if conversation_id not in self._cookie_fail_count:
            self._cookie_fail_count[conversation_id] = 0
        self._cookie_fail_count[conversation_id] += 1

        logger.info(f"Cookie {conversation_id} 失败次数: {self._cookie_fail_count[conversation_id]}")

        # 如果失败次数达到上限，加入黑名单
        if self._cookie_fail_count[conversation_id] >= self._max_failures:
            self._add_to_blacklist(conversation_id)
            return True
        return False

    def _is_rate_limit_error(self, data: dict) -> bool:
        """检查是否是次数限制错误"""
        if data.get('code') == 500:
            text_msg = data.get('text', '')
            if '今天的生成次数已达到上限' in text_msg or '明天再来免费生成' in text_msg:
                return True
        return False

    # ---------------- 修复的核心API调用方法 ----------------
    async def _call_doubao_api_with_retry(self, desc, image_url=None, max_retries=3):
        """调用API，支持Cookie轮询和黑名单 - 修复版本"""
        available_pairs = self._get_available_cookie_pairs()
        if not available_pairs:
            logger.error("没有可用的Cookie对")
            return []

        attempts = 0
        total_cookie_attempts = 0
        max_total_attempts = len(available_pairs) * 3  # 防止无限循环

        while total_cookie_attempts < max_total_attempts:
            # 重置速率限制标志
            self._last_rate_limit = False

            cookie_pair = self._get_current_cookie_pair()
            if not cookie_pair:
                logger.error("无法获取Cookie对")
                return []

            conv_id = cookie_pair['conversation_id']
            logger.info(f"尝试使用Cookie对 {self.current_cookie_index}: {conv_id}")

            urls, rate_limited = await self._call_doubao_api(desc, image_url, cookie_pair)
            attempts += 1
            total_cookie_attempts += 1

            # 关键修复：如果遇到次数限制，立即加入黑名单并切换
            if rate_limited:
                logger.warning(f"Cookie对 {conv_id} 达到次数限制，立即加入黑名单")
                self._add_to_blacklist(conv_id)
                # 切换到下一个Cookie
                if not self._switch_to_next_cookie():
                    logger.error("所有Cookie对都已尝试，无法继续")
                    return []
                # 重置尝试次数，用新的Cookie重新开始
                attempts = 0
                continue

            # 如果API调用成功，返回结果并重置失败计数
            if urls:
                logger.info(f"使用Cookie对 {self.current_cookie_index} 成功生成图片")
                # 成功时重置该Cookie的失败计数
                if conv_id in self._cookie_fail_count:
                    self._cookie_fail_count[conv_id] = 0
                return urls

            # 记录失败次数
            should_blacklist = self._record_failure(conv_id)

            # 检查是否需要切换Cookie（次数限制或失败过多）
            if self._last_rate_limit or should_blacklist:
                reason = "次数限制" if self._last_rate_limit else "失败次数过多"
                logger.warning(f"Cookie对 {self.current_cookie_index} {reason}，尝试切换")
                if not self._switch_to_next_cookie():
                    logger.error("所有Cookie对都已尝试，无法继续")
                    return []
                # 重置尝试次数，用新的Cookie重新开始
                attempts = 0
                continue

            # 其他错误，等待后重试当前Cookie
            if attempts < max_retries:
                wait_time = 2
                logger.warning(f"API调用失败，等待{wait_time}秒后继续尝试...")
                await asyncio.sleep(wait_time)
            else:
                # 当前Cookie重试次数用完，切换到下一个
                logger.warning(f"Cookie对 {self.current_cookie_index} 重试次数用完，尝试切换")
                if not self._switch_to_next_cookie():
                    logger.error("所有Cookie对都已尝试，无法继续")
                    return []
                attempts = 0

        logger.error(f"所有Cookie对尝试{total_cookie_attempts}次均失败")
        return []

    async def _call_doubao_api(self, desc, image_url=None, cookie_pair=None):
        """调用豆包API，返回 (图片URL列表, 是否达到次数限制)"""
        if not cookie_pair:
            cookie_pair = self._get_current_cookie_pair()
            if not cookie_pair:
                return [], False

        params = {
            'description': desc,
            'type': self.current_style,
            'ratio': self.current_ratio,
            'model': self.current_model,
            'conversation_id': cookie_pair['conversation_id'],
            'Cookie': cookie_pair['cookie'],
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

                    if resp.status != 200:
                        logger.error(f"API返回非200状态: {resp.status}")
                        return [], False

                    try:
                        data = json.loads(text)
                        logger.info(f"API响应JSON: {json.dumps(data, ensure_ascii=False)}")

                        # 关键修复：检查是否达到次数限制
                        if self._is_rate_limit_error(data):
                            logger.warning(
                                f"Cookie对 {cookie_pair['conversation_id']} 达到次数限制: {data.get('text', '')}")
                            return [], True  # 返回空列表和达到限制标志

                        if data.get('code') == 200 and 'image_url' in data:
                            urls = data['image_url']
                            if isinstance(urls, list):
                                logger.info(f"成功获取到 {len(urls)} 张图片")
                                return urls, False
                            elif isinstance(urls, str):
                                logger.info("成功获取到1张图片")
                                return [urls], False
                        else:
                            error_code = data.get('code', '未知')
                            error_msg = data.get('message', '未知错误')
                            logger.error(f"API业务错误: code={error_code}, message={error_msg}")
                            return [], False

                    except json.JSONDecodeError as e:
                        logger.error(f"JSON解析失败: {e}, 响应内容: {text}")
                        return [], False

        except aiohttp.ClientError as e:
            logger.error(f"网络请求失败: {e}")
            return [], False
        except asyncio.TimeoutError:
            logger.error("API请求超时")
            return [], False
        except Exception as e:
            logger.error(f"API调用未知错误: {e}")
            return [], False

    # ---------------- 修复清空黑名单命令 ----------------
    @filter.command("清空黑名单", prefix_optional=True)
    async def clear_blacklist(self, event: AstrMessageEvent):
        """清空Cookie黑名单 - 修复版本：释放Cookie而不是删除"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以清空黑名单")
            return

        blacklist_count = len(self._cookie_blacklist)

        # 关键修复：从黑名单中移除所有Cookie，但不删除它们
        for conversation_id in list(self._cookie_blacklist):
            self._remove_from_blacklist(conversation_id)

        # 清空失败计数
        self._cookie_fail_count.clear()

        yield event.plain_result(f"✅ 已清空黑名单，释放了 {blacklist_count} 个Cookie\n现在所有Cookie都可以重新使用")

    # ---------------- 修复移除黑名单命令 ----------------
    @filter.command("移除黑名单", prefix_optional=True)
    async def remove_from_blacklist(self, event: AstrMessageEvent):
        """从黑名单中移除指定Cookie - 修复版本"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以操作黑名单")
            return

        conversation_id = event.message_str.strip()
        if not conversation_id:
            yield event.plain_result("请提供要移除的conversation_id")
            return

        if conversation_id in self._cookie_blacklist:
            self._remove_from_blacklist(conversation_id)
            if conversation_id in self._cookie_fail_count:
                del self._cookie_fail_count[conversation_id]

            yield event.plain_result(f"✅ 已从黑名单释放: {conversation_id}\n该Cookie现在可以重新使用")
        else:
            yield event.plain_result(f"❌ 未在黑名单中找到: {conversation_id}")

    # ---------------- 修复cookie列表显示 ----------------
    @filter.command("cookie列表", prefix_optional=True)
    async def list_cookies(self, event: AstrMessageEvent):
        """显示所有Cookie对 - 修复版本：显示黑名单状态"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以查看Cookie列表")
            return

        if not self.cookie_pairs:
            yield event.plain_result("📝 暂未配置任何Cookie对")
            return

        available_pairs = self._get_available_cookie_pairs()

        msg = "🍪 Cookie对列表:\n\n"
        for i, pair in enumerate(self.cookie_pairs):
            status = "✅ 当前使用" if (
                        i < len(available_pairs) and available_pairs[i] == self._get_current_cookie_pair()) else "🔄 备用"
            if pair.get('in_blacklist', False):
                status = "🚫 黑名单中"
            fail_count = self._cookie_fail_count.get(pair['conversation_id'], 0)
            msg += f"{i + 1}. {pair['conversation_id']} {status} (失败{fail_count}次)\n"

        msg += f"\n📊 统计信息:"
        msg += f"\n• 总Cookie对: {len(self.cookie_pairs)} 个"
        msg += f"\n• 可用Cookie: {len(available_pairs)} 个"
        msg += f"\n• 黑名单Cookie: {len(self._cookie_blacklist)} 个"
        msg += f"\n• 当前使用索引: {self.current_cookie_index}"

        current_pair = self._get_current_cookie_pair()
        if current_pair:
            msg += f"\n• 当前使用: {current_pair['conversation_id']}"
        else:
            msg += f"\n• 当前使用: 无可用Cookie"

        yield event.plain_result(msg)

    # ---------------- 修复调试命令 ----------------
    @filter.command("测试cookie", prefix_optional=True)
    async def debug_cookies(self, event: AstrMessageEvent):
        """调试Cookie对信息 - 修复版本"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以查看调试信息")
            return

        available_pairs = self._get_available_cookie_pairs()

        msg = "🔍 Cookie系统调试信息:\n\n"

        # 显示配置来源
        config = self.context.get_config()
        cookie_list_from_config = config.get("cookie_list", [])
        msg += f"📋 从配置读取的cookie_list: {cookie_list_from_config}\n\n"

        # 显示所有Cookie对（包括黑名单中的）
        msg += "📋 所有Cookie对:\n"
        for i, pair in enumerate(self.cookie_pairs):
            status = "🟢 可用" if not pair.get('in_blacklist', False) else "🔴 黑名单"
            if i < len(available_pairs) and available_pairs[i] == self._get_current_cookie_pair():
                status = "🟢 当前使用"
            fail_count = self._cookie_fail_count.get(pair['conversation_id'], 0)
            msg += f"\n{i + 1}. {status}\n"
            msg += f"   conversation_id: {pair['conversation_id']}\n"
            msg += f"   失败次数: {fail_count}\n"
            msg += f"   黑名单: {pair.get('in_blacklist', False)}\n"
            msg += f"   cookie: {pair['cookie'][:50]}...\n"

        msg += f"\n📊 统计信息:"
        msg += f"\n• 总Cookie对: {len(self.cookie_pairs)}个"
        msg += f"\n• 可用Cookie: {len(available_pairs)}个"
        msg += f"\n• 黑名单Cookie: {len(self._cookie_blacklist)}个"
        msg += f"\n• 当前使用索引: {self.current_cookie_index}"

        current_pair = self._get_current_cookie_pair()
        if current_pair:
            msg += f"\n• 当前使用: {current_pair['conversation_id']}"
        else:
            msg += f"\n• 当前使用: 无可用Cookie"

        yield event.plain_result(msg)

    # ---------------- 以下是你原有的其他代码保持不变 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_prompt_command(self, event: AstrMessageEvent):
        """统一处理所有自定义提示词命令 - 高优先级"""
        text = event.message_str.strip()
        if not text:
            return

        # 获取第一个词作为命令
        cmd = text.split()[0].strip()

        # 检查是否是自定义提示词命令
        if cmd not in self.prompt_map:
            return

        # 找到对应的提示词
        actual_prompt = self.prompt_map[cmd]
        logger.info(f"收到自定义提示词命令: /{cmd}, 实际提示词: {actual_prompt}")

        # 获取图片（支持所有方式：直接图片、引用图片、@用户、GIF）
        image_url = await self._get_image_url_from_event(event)

        # 如果没找到图片，尝试从@用户获取头像
        if not image_url:
            for seg in event.message_obj.message:
                if isinstance(seg, At):
                    qq = str(seg.qq)
                    image_url = f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640"
                    logger.info(f"使用@用户头像作为图片源: {qq}")
                    break

        if not image_url:
            yield event.plain_result("❌ 请发送图片、引用图片或@用户来使用此提示词")
            event.stop_event()
            return

        display_desc = f"[{cmd}] {actual_prompt[:50]}..." if len(
            actual_prompt) > 50 else f"[{cmd}] {actual_prompt}"

        yield event.plain_result(
            f"🎨 正在基于图片生成...\n提示词：{display_desc}\n风格：{self.current_style}\n比例：{self.current_ratio}\n模型：{self.current_model}"
        )

        success, error_msg = await self._complete_image_generation_with_retry(event, actual_prompt, image_url)
        if not success and error_msg:
            yield event.plain_result(error_msg)

        event.stop_event()


    # ---------------- 修复原有的 /jm 命令 ----------------
    @filter.command("jm")
    async def image_to_image(self, event: AstrMessageEvent):
        """图生图命令 - 检查是否被自定义提示词拦截"""
        msg_str = event.message_obj.message_str
        parts = msg_str.split(" ", 1)  # 只分割一次，保留完整的描述词
        if len(parts) < 2:
            yield event.plain_result("使用方法：/jm <描述词> + 图片/@用户/引用图片")
            return

        desc = parts[1].strip()

        # 检查是否是自定义提示词，如果是则直接返回（因为会被事件监听器处理）
        if desc in self.prompt_map:
            return

        # 如果不是自定义提示词，继续原有逻辑
        actual_prompt = desc
        display_desc = desc

        image_url = await self._get_image_url_from_event(event)

        if not image_url:
            # 检查是否有@用户
            for seg in event.message_obj.message:
                if isinstance(seg, At):
                    qq = str(seg.qq)
                    image_url = f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640"
                    break

        if not image_url:
            yield event.plain_result("❌ 没有找到图片，请发送图片或@用户或引用图片/GIF")
            return

        yield event.plain_result(
            f"🎨 正在基于图片生成...\n描述：{display_desc}\n风格：{self.current_style}\n比例：{self.current_ratio}\n模型：{self.current_model}"
        )

        success, error_msg = await self._complete_image_generation_with_retry(event, actual_prompt, image_url)
        if not success and error_msg:
            yield event.plain_result(error_msg)

    # ---------------- 修复原有的 /db 命令 ----------------
    @filter.command("db")
    async def generate_image(self, event: AstrMessageEvent):
        """文生图命令 - 检查是否被自定义提示词拦截"""
        msg_str = event.message_obj.message_str
        parts = msg_str.split(" ", 1)  # 只分割一次，保留完整的描述词
        if len(parts) < 2:
            yield event.plain_result("使用方法：/db <描述词>\n例如：/db 一只可爱的猫")
            return

        desc = parts[1].strip()

        # 检查是否是自定义提示词，如果是则直接返回
        if desc in self.prompt_map:
            return

        # 如果不是自定义提示词，继续原有逻辑
        actual_prompt = desc
        display_desc = desc

        yield event.plain_result(
            f"🎨 正在生成图片...\n描述：{display_desc}\n风格：{self.current_style}\n比例：{self.current_ratio}\n模型：{self.current_model}"
        )

        success, error_msg = await self._complete_image_generation_with_retry(event, actual_prompt)
        if not success and error_msg:
            yield event.plain_result(error_msg)

    # ---------------- Cookie对管理命令 ----------------
    @filter.command("添加cookie", prefix_optional=True)
    async def add_cookie(self, event: AstrMessageEvent):
        """添加Cookie对"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以添加Cookie对")
            return

        raw = event.message_str.strip()
        if ":" not in raw:
            yield event.plain_result('格式错误, 正确示例:\n#添加cookie 123456789:your_cookie_value_here')
            return

        conversation_id, cookie = map(str.strip, raw.split(":", 1))

        # 更新配置
        config = self.context.get_config()
        cookie_list = config.get("cookie_list", [])

        # 检查是否已存在，存在则更新，否则添加
        found = False
        for idx, item in enumerate(cookie_list):
            if item.strip().startswith(conversation_id + ":"):
                cookie_list[idx] = f"{conversation_id}:{cookie}"
                found = True
                break
        if not found:
            cookie_list.append(f"{conversation_id}:{cookie}")

        # 保存配置并重新加载
        await config.set("cookie_list", cookie_list)
        self._load_cookie_pairs(config)

        yield event.plain_result(f"✅ 已保存Cookie对: {conversation_id}")

    @filter.command("cookie列表", prefix_optional=True)
    async def list_cookies(self, event: AstrMessageEvent):
        """显示所有Cookie对"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以查看Cookie列表")
            return

        if not self.cookie_pairs:
            yield event.plain_result("📝 暂未配置任何Cookie对")
            return

        msg = "🍪 Cookie对列表:\n\n"
        for i, pair in enumerate(self.cookie_pairs):
            status = "✅ 当前使用" if i == self.current_cookie_index else "🔄 备用"
            msg += f"{i + 1}. {pair['conversation_id']} {status}\n"

        msg += f"\n共 {len(self.cookie_pairs)} 组Cookie对"
        msg += f"\n当前使用: 第 {self.current_cookie_index + 1} 组"
        yield event.plain_result(msg)

    @filter.command("删除cookie", prefix_optional=True)
    async def delete_cookie(self, event: AstrMessageEvent):
        """删除Cookie对"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以删除Cookie对")
            return

        conversation_id = event.message_str.strip()
        if not conversation_id:
            yield event.plain_result("请提供要删除的conversation_id")
            return

        config = self.context.get_config()
        cookie_list = config.get("cookie_list", [])

        new_cookie_list = []
        deleted = False

        for item in cookie_list:
            if not item.strip().startswith(conversation_id + ":"):
                new_cookie_list.append(item)
            else:
                deleted = True

        if deleted:
            await config.set("cookie_list", new_cookie_list)
            self._load_cookie_pairs(config)
            # 如果删除的是当前使用的cookie，重置索引
            if self.current_cookie_index >= len(self.cookie_pairs):
                self.current_cookie_index = 0
            yield event.plain_result(f"✅ 已删除Cookie对: {conversation_id}")
        else:
            yield event.plain_result(f"❌ 未找到Cookie对: {conversation_id}")

    # ---------------- 新增Cookie黑名单管理命令 ----------------
    @filter.command("cookie黑名单", prefix_optional=True)
    async def show_blacklist(self, event: AstrMessageEvent):
        """显示Cookie黑名单"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以查看黑名单")
            return

        if not self._cookie_blacklist:
            yield event.plain_result("✅ 黑名单为空")
            return

        msg = "🚫 Cookie黑名单:\n\n"
        for i, conv_id in enumerate(self._cookie_blacklist, 1):
            fail_count = self._cookie_fail_count.get(conv_id, 0)
            msg += f"{i}. {conv_id} (失败{fail_count}次)\n"

        msg += f"\n共 {len(self._cookie_blacklist)} 个Cookie在黑名单中"
        yield event.plain_result(msg)

    @filter.command("清空黑名单", prefix_optional=True)
    async def clear_blacklist(self, event: AstrMessageEvent):
        """清空Cookie黑名单"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以清空黑名单")
            return

        blacklist_count = len(self._cookie_blacklist)
        self._cookie_blacklist.clear()
        self._cookie_fail_count.clear()

        # 重新加载Cookie对（包含之前黑名单中的）
        config = self.context.get_config()
        self._load_cookie_pairs(config)

        yield event.plain_result(f"✅ 已清空黑名单，恢复了 {blacklist_count} 个Cookie")

    @filter.command("移除黑名单", prefix_optional=True)
    async def remove_from_blacklist(self, event: AstrMessageEvent):
        """从黑名单中移除指定Cookie"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以操作黑名单")
            return

        conversation_id = event.message_str.strip()
        if not conversation_id:
            yield event.plain_result("请提供要移除的conversation_id")
            return

        if conversation_id in self._cookie_blacklist:
            self._cookie_blacklist.remove(conversation_id)
            if conversation_id in self._cookie_fail_count:
                del self._cookie_fail_count[conversation_id]

            # 重新加载Cookie对
            config = self.context.get_config()
            self._load_cookie_pairs(config)

            yield event.plain_result(f"✅ 已从黑名单移除: {conversation_id}")
        else:
            yield event.plain_result(f"❌ 未在黑名单中找到: {conversation_id}")

    # ---------------- 修改调试命令，显示黑名单信息 ----------------
    @filter.command("测试cookie", prefix_optional=True)
    async def debug_cookies(self, event: AstrMessageEvent):
        """调试Cookie对信息"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 只有管理员可以查看调试信息")
            return

        msg = "🔍 Cookie系统调试信息:\n\n"

        # 显示配置来源
        config = self.context.get_config()
        cookie_list_from_config = config.get("cookie_list", [])
        msg += f"📋 从配置读取的cookie_list: {cookie_list_from_config}\n\n"

        # 显示可用Cookie对
        msg += "✅ 可用Cookie对:\n"
        for i, pair in enumerate(self.cookie_pairs):
            status = "🟢 当前使用" if i == self.current_cookie_index else "🟡 备用"
            fail_count = self._cookie_fail_count.get(pair['conversation_id'], 0)
            msg += f"\n{i + 1}. {status}\n"
            msg += f"   conversation_id: {pair['conversation_id']}\n"
            msg += f"   失败次数: {fail_count}\n"
            msg += f"   cookie: {pair['cookie'][:50]}...\n"

        # 显示黑名单
        msg += f"\n🚫 黑名单 ({len(self._cookie_blacklist)}个):\n"
        if self._cookie_blacklist:
            for i, conv_id in enumerate(self._cookie_blacklist, 1):
                fail_count = self._cookie_fail_count.get(conv_id, 0)
                msg += f"{i}. {conv_id} (失败{fail_count}次)\n"
        else:
            msg += "   空\n"

        msg += f"\n📊 统计信息:"
        msg += f"\n• 可用Cookie: {len(self.cookie_pairs)}个"
        msg += f"\n• 黑名单Cookie: {len(self._cookie_blacklist)}个"
        msg += f"\n• 当前使用索引: {self.current_cookie_index}"
        msg += f"\n• 当前conversation_id: {self.cookie_pairs[self.current_cookie_index]['conversation_id'] if self.cookie_pairs else '无'}"

        yield event.plain_result(msg)

    # ---------------- 自定义提示词管理命令 ----------------
    @filter.command("添加提示词", prefix_optional=True)
    async def add_prompt(self, event: AstrMessageEvent):
        """添加自定义提示词"""
        raw = event.message_str.strip()
        if ":" not in raw:
            yield event.plain_result('格式错误, 正确示例:\n#添加提示词 动漫化:将图片转换为动漫风格')
            return

        key, new_value = map(str.strip, raw.split(":", 1))

        # 更新配置
        config = self.context.get_config()
        prompt_list = config.get("prompt_list", [])

        # 检查是否已存在，存在则更新，否则添加
        found = False
        for idx, item in enumerate(prompt_list):
            if item.strip().startswith(key + ":"):
                prompt_list[idx] = f"{key}:{new_value}"
                found = True
                break
        if not found:
            prompt_list.append(f"{key}:{new_value}")

        # 保存配置并重新加载
        await config.set("prompt_list", prompt_list)
        self._load_prompt_map(config)

        yield event.plain_result(f"✅ 已保存提示词: {key}\n现在可以使用 /{key} 命令")

    @filter.command("提示词列表", prefix_optional=True)
    async def list_prompts(self, event: AstrMessageEvent):
        """显示所有自定义提示词"""
        if not self.prompt_map:
            yield event.plain_result("📝 暂未配置任何自定义提示词")
            return

        msg = "🎨 自定义提示词命令列表:\n\n"
        for key, value in self.prompt_map.items():
            # 只显示前30个字符避免消息过长
            preview = value[:30] + "..." if len(value) > 30 else value
            msg += f"• /{key}: {preview}\n"

        msg += f"\n共 {len(self.prompt_map)} 个提示词命令"
        msg += "\n\n使用方式:"
        msg += "\n1. /手办化 + 图片"
        msg += "\n2. /手办化 + 引用图片"
        msg += "\n3. /手办化 + @用户"
        msg += "\n4. /手办化 + GIF图片"
        yield event.plain_result(msg)

    @filter.command("删除提示词", prefix_optional=True)
    async def delete_prompt(self, event: AstrMessageEvent):
        """删除自定义提示词"""
        key = event.message_str.strip()
        if not key:
            yield event.plain_result("请提供要删除的提示词关键词")
            return

        config = self.context.get_config()
        prompt_list = config.get("prompt_list", [])

        new_prompt_list = []
        deleted = False

        for item in prompt_list:
            if not item.strip().startswith(key + ":"):
                new_prompt_list.append(item)
            else:
                deleted = True

        if deleted:
            await config.set("prompt_list", new_prompt_list)
            self._load_prompt_map(config)
            yield event.plain_result(f"✅ 已删除提示词: {key}")
        else:
            yield event.plain_result(f"❌ 未找到提示词: {key}")

    # ---------------- 设置命令 ----------------
    @filter.command("db设置")
    async def show_settings(self, event: AstrMessageEvent):
        prompt_keys = "、".join([f"/{key}" for key in self.prompt_map.keys()]) if self.prompt_map else "暂无"
        cookie_count = len(self.cookie_pairs)
        blacklist_count = len(self._cookie_blacklist)

        info = f"""🎨 当前绘画设置：
风格：{self.current_style}
比例：{self.current_ratio}
模型：{self.current_model}
可用Cookie对：{cookie_count} 组
黑名单Cookie：{blacklist_count} 个
自定义提示词命令：{len(self.prompt_map)} 个

📝 当前可用命令：
{prompt_keys}

🍪 Cookie管理：
#添加cookie <conversation_id:cookie>
#cookie列表
#删除cookie <conversation_id>
#cookie黑名单
#清空黑名单
#移除黑名单 <conversation_id>

🎯 使用方式：
1. 提示词命令（图生图）：
   /手办化 + 图片
   /手办化 + 引用图片
   /手办化 + @用户
   /手办化 + GIF图片

2. 传统命令方式：
   /db <描述词> - 文生图
   /jm <描述词> - 图生图

3. 设置命令：
   /切换风格 <风格>
   /切换比例 <比例>  
   /切换模型 <模型>

4. 提示词管理：
   /添加提示词 <关键词:描述>
   /提示词列表
   /删除提示词 <关键词>"""
        yield event.plain_result(info)

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

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """检查是否是管理员"""
        admin_ids = self.context.get_config().get("admins_id", [])
        return event.get_sender_id() in admin_ids

    # ---------------- 以下是你原有的其他方法保持不变 ----------------
    async def _get_image_url_from_event(self, event: AstrMessageEvent) -> str:
        """统一处理直接图片/引用图片/GIF"""
        chain = event.message_obj.message

        # 1. 首先检查直接图片
        for seg in chain:
            if isinstance(seg, Image):
                url = await self._process_image_segment(seg)
                if url:
                    return url

        # 2. 检查引用回复中的图片
        for seg in chain:
            if isinstance(seg, Reply):
                reply_chain = getattr(seg, 'chain', [])
                for reply_seg in reply_chain:
                    if isinstance(reply_seg, Image):
                        url = await self._process_image_segment(reply_seg)
                        if url:
                            return url

        # 3. 检查@用户
        for seg in chain:
            if isinstance(seg, At):
                qq = str(seg.qq)
                return f"https://q.qlogo.cn/g?b=qq&nk={qq}&s=640"

        return ""

    async def _process_image_segment(self, image_seg: Image) -> str:
        """处理图片消息段"""
        try:
            if hasattr(image_seg, 'url') and image_seg.url:
                url = image_seg.url
                if url.lower().endswith('.gif'):
                    base64_data = await self._gif_to_base64(url)
                    if base64_data:
                        return base64_data
                return url
            elif hasattr(image_seg, 'file') and image_seg.file:
                file_path = image_seg.file
                base64_data = await self._file_to_base64(file_path)
                if base64_data:
                    return base64_data
            elif hasattr(image_seg, 'base64') and image_seg.base64:
                return f"data:image/png;base64,{image_seg.base64}"
        except Exception as e:
            logger.error(f"处理图片消息段时出错: {e}")
        return ""

    async def _send_first_image(self, event: AstrMessageEvent, url: str, max_retries=10):
        for attempt in range(max_retries):
            try:
                await event.send(event.chain_result([Plain("🖼️ 图片 1：\n"), Image.fromURL(url)]))
                return True
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
        return False

    async def _send_remaining_images(self, event: AstrMessageEvent, urls: list, max_retries=10):
        if not urls or len(urls) <= 1:
            return True

        remaining_urls = urls[1:]
        for attempt in range(max_retries):
            try:
                chain_components = [Plain("🖼️ 其他图片：\n")]
                for i, url in enumerate(remaining_urls, 2):
                    chain_components.extend([
                        Plain(f"\n图片 {i}：\n"),
                        Image.fromURL(url)
                    ])
                await event.send(event.chain_result(chain_components))
                return True
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(2)
        return False

    async def _send_image_urls_with_retry(self, event: AstrMessageEvent, urls, max_retries=10):
        if not urls:
            return False
        first_success = await self._send_first_image(event, urls[0], max_retries)
        if len(urls) > 1 and first_success:
            await self._send_remaining_images(event, urls, max_retries)
        return first_success


    async def _complete_image_generation_with_retry(self, event: AstrMessageEvent, desc, image_url=None, max_retries=2):
        for attempt in range(max_retries + 1):
            try:
                urls = await self._call_doubao_api_with_retry(desc, image_url)
                if not urls:
                    if attempt < max_retries:
                        await asyncio.sleep(3)
                        continue
                    else:
                        return False, "❌ 图片生成失败，可能是生成时间过长或网络问题"

                success = await self._send_image_urls_with_retry(event, urls)
                if success:
                    return True, None
                else:
                    if attempt < max_retries:
                        await asyncio.sleep(3)
                        continue
                    else:
                        return False, "❌ 图片发送失败，请稍后重试"
            except Exception as e:
                logger.error(f"完整流程异常: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(3)
                    continue
                else:
                    return False, "❌ 图片处理异常，请稍后重试"
        return False, "❌ 图片生成失败，请稍后重试"

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
        except Exception:
            pass
        return ""
    async def _file_to_base64(self, file_path: str) -> str:
        try:
            if os.path.exists(file_path):
                with open(file_path, 'rb') as f:
                    file_bytes = f.read()
                ext = os.path.splitext(file_path)[1].lower()
                mime_type = f"image/{ext[1:]}" if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp'] else "image/jpeg"
                return f"data:{mime_type};base64,{base64.b64encode(file_bytes).decode()}"
        except Exception:
            pass
        return ""

    def _extract_qq_from_at(self, text: str) -> str:
        m = re.search(r'\d+', text)
        return m.group(0) if m else None

    async def terminate(self):
        logger.info("豆包AI绘画插件已卸载")