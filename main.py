"""
astrbot_plugin_deepseek_balance
每日定时查询 AstrBot 配置中的 DeepSeek Provider 账户余额。

功能：
- 从 `config_path`（默认 ~/.config/astrbot/config.json）读取 providers，
  匹配关键字（默认 "deepseek"），取其 api_key。
- 在配置的时间点（默认每日 06:00 / 18:00）调用
  https://api.deepseek.com/user/balance 查询余额。
- 当换算后的人民币余额 <= 阈值（默认 70.00 CNY）时，向配置的管理群 /
  管理员私聊发送告警，告警文案可由 LLM 以当前人设生成。
- 若未配置 DeepSeek Provider，插件自动跳过，不执行任何查询。
- 检测到 LLM 因余额耗尽无法使用时，向管理员发送自定义告警文案。
- 提供手动查询指令，供管理员随时查看当前余额。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from typing import Any, Optional

try:
    import aiohttp  # type: ignore
except Exception:  # 运行环境不一定有 aiohttp，退化到 urllib
    aiohttp = None

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_deepseek_balance"

DEFAULT_CONFIG_PATH = "~/.config/astrbot/config.json"
DEFAULT_PROVIDER_KEYWORD = "deepseek"
DEFAULT_ENDPOINT = "https://api.deepseek.com/user/balance"
DEFAULT_THRESHOLD_CNY = 70.00
DEFAULT_USD_TO_CNY_RATE = 7.2
DEFAULT_CHECK_TIMES = ["06:00", "18:00"]
DEFAULT_COOLDOWN_MINUTES = 30
DEFAULT_DEPLETED_MESSAGE = "API余额已经用完，请管理者及时充值。"
DATA_FILENAME = "deepseek_balance_data.json"


class DeepSeekBalancePlugin(Star):
    """DeepSeek 余额监控插件。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._data_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), DATA_FILENAME
        )
        # 运行时状态
        self._state: dict[str, Any] = {
            "last_check_ts": 0.0,
            "last_balance_usd": None,
            "last_balance_cny": None,
            "last_status": "init",  # init | ok | low | error | no_provider
            "last_status_msg": "",
            "last_alert_ts": 0.0,  # 上次告警时间，用于冷却
            "last_alert_bucket": "",  # 告警分桶（按余额区间）
            "last_depleted_warn_ts": 0.0,  # 上次 LLM 耗尽告警时间
            "provider_api_key": "",
            "provider_id": "",
            "provider_name": "",
        }
        self._load_state()
        self._scan_provider()

        self._scheduler_task: Optional[asyncio.Task] = None
        self._pending_alerts: list[str] = []  # 未成功送达的告警文本队列

    # ------------------------------------------------------------------ 生命周期

    @filter.on_astrbot_loaded()
    async def _on_loaded(self):
        """AstrBot 加载完成后启动后台调度任务。"""
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def terminate(self):
        """插件卸载时取消调度任务并保存状态。"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        self._save_state()

    # ------------------------------------------------------------------ 配置 / Provider

    def _resolve_config_path(self) -> str:
        path = self.config.get("config_path") or DEFAULT_CONFIG_PATH
        if isinstance(path, str) and path.startswith("~"):
            path = os.path.expanduser(path)
        return path

    def _scan_provider(self) -> None:
        """扫描 AstrBot config.json，定位 DeepSeek Provider 并提取 api_key。"""
        cfg_path = self._resolve_config_path()
        keyword = (
            self.config.get("provider_keyword") or DEFAULT_PROVIDER_KEYWORD
        ).lower()
        self._state["provider_api_key"] = ""
        self._state["provider_id"] = ""
        self._state["provider_name"] = ""
        if not cfg_path or not os.path.exists(cfg_path):
            logger.warning(
                f"[DeepSeek 余额] AstrBot 配置文件不存在: {cfg_path}，跳过。"
            )
            self._state["last_status"] = "no_provider"
            self._state["last_status_msg"] = f"配置文件不存在: {cfg_path}"
            return
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                astrbot_cfg = json.load(f)
        except Exception as e:
            logger.warning(f"[DeepSeek 余额] 读取配置文件失败: {e}")
            self._state["last_status"] = "no_provider"
            self._state["last_status_msg"] = f"读取配置失败: {e}"
            return

        providers = astrbot_cfg.get("providers") or {}
        if not isinstance(providers, dict):
            self._state["last_status"] = "no_provider"
            self._state["last_status_msg"] = "providers 结构异常"
            return

        for pid, pcfg in providers.items():
            if not isinstance(pcfg, dict):
                continue
            pid_str = str(pid).lower()
            pname = str(pcfg.get("name") or "").lower()
            ptype = str(pcfg.get("type") or "").lower()
            endpoint = str(pcfg.get("endpoint") or "").lower()
            if keyword in pid_str or keyword in pname or keyword in ptype or keyword in endpoint:
                api_key = (
                    pcfg.get("api_key")
                    or pcfg.get("apiKey")
                    or pcfg.get("key")
                    or pcfg.get("token")
                    or ""
                )
                if not api_key:
                    continue
                self._state["provider_api_key"] = str(api_key)
                self._state["provider_id"] = str(pid)
                self._state["provider_name"] = str(pcfg.get("name") or pid)
                logger.info(
                    f"[DeepSeek 余额] 识别到 Provider id={pid} name={pcfg.get('name')}"
                )
                return

        logger.info(
            f"[DeepSeek 余额] 未在 {cfg_path} 中找到包含关键字 '{keyword}' 的 Provider，跳过。"
        )
        self._state["last_status"] = "no_provider"
        self._state["last_status_msg"] = (
            f"未在 {cfg_path} 中找到包含关键字 '{keyword}' 的 Provider"
        )

    def _has_provider(self) -> bool:
        return bool(self._state.get("provider_api_key"))

    def _get_threshold_cny(self) -> float:
        """获取人民币告警阈值。"""
        try:
            return float(
                self.config.get("threshold_cny", DEFAULT_THRESHOLD_CNY)
                or DEFAULT_THRESHOLD_CNY
            )
        except (TypeError, ValueError):
            return DEFAULT_THRESHOLD_CNY

    def _get_usd_to_cny_rate(self) -> float:
        """获取美元兑人民币汇率。"""
        try:
            rate = float(
                self.config.get("usd_to_cny_rate", DEFAULT_USD_TO_CNY_RATE)
                or DEFAULT_USD_TO_CNY_RATE
            )
            return rate if rate > 0 else DEFAULT_USD_TO_CNY_RATE
        except (TypeError, ValueError):
            return DEFAULT_USD_TO_CNY_RATE

    # ------------------------------------------------------------------ 调度

    async def _scheduler_loop(self) -> None:
        """按本地时间每日在配置的时间点触发查询。"""
        try:
            await asyncio.sleep(5)  # 等待 AstrBot 完全就绪
        except asyncio.CancelledError:
            return

        last_run_slot: str = ""
        while True:
            try:
                now = datetime.now()
                slots = self._parse_check_times()
                slot = now.strftime("%H:%M")
                if slot in slots and slot != last_run_slot:
                    last_run_slot = slot
                    logger.info(f"[DeepSeek 余额] 到达检查时间 {slot}，开始查询。")
                    asyncio.create_task(self._do_check(trigger="schedule"))
                # 计算到下一分钟的秒数，避免长时间挂起
                await asyncio.sleep(max(5, 60 - now.second))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[DeepSeek 余额] 调度循环异常: {e}")
                await asyncio.sleep(30)

    def _parse_check_times(self) -> set[str]:
        raw = self.config.get("check_times") or DEFAULT_CHECK_TIMES
        out: set[str] = set()
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raw = []
        for t in raw:
            if not isinstance(t, str):
                continue
            t = t.strip()
            if not t:
                continue
            try:
                parts = t.split(":")
                hh = int(parts[0])
                mm = int(parts[1]) if len(parts) > 1 else 0
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    out.add(f"{hh:02d}:{mm:02d}")
            except Exception:
                continue
        if not out:
            out = {"06:00", "18:00"}
        return out

    # ------------------------------------------------------------------ 查询

    async def _do_check(self, trigger: str = "manual") -> dict[str, Any]:
        """执行一次余额查询。返回状态摘要。"""
        self._scan_provider()
        result: dict[str, Any] = {
            "ok": False,
            "trigger": trigger,
            "balance_usd": None,
            "balance_cny": None,
            "reason": "",
        }
        if not self._has_provider():
            result["reason"] = "no_provider"
            self._state["last_status"] = "no_provider"
            self._state["last_status_msg"] = self._state.get(
                "last_status_msg"
            ) or "未配置 DeepSeek Provider"
            self._state["last_check_ts"] = time.time()
            self._save_state()
            return result

        endpoint = self.config.get("api_endpoint") or DEFAULT_ENDPOINT
        try:
            timeout = float(self.config.get("request_timeout", 15) or 15)
        except (TypeError, ValueError):
            timeout = 15.0

        api_key = self._state["provider_api_key"]
        payload = None
        err_text = ""
        try:
            payload = await self._http_get(endpoint, api_key, timeout)
        except asyncio.TimeoutError:
            err_text = f"请求超时（{timeout}s）"
        except Exception as e:  # noqa: BLE001
            err_text = f"请求异常: {e}"

        balance_usd: Optional[float] = None
        if isinstance(payload, dict):
            balance_usd = self._extract_balance(payload)
            if balance_usd is None:
                err_text = self._extract_error(payload) or "返回字段异常"
        elif payload is not None:
            err_text = f"响应格式异常: {payload}"

        rate = self._get_usd_to_cny_rate()
        balance_cny = None
        if balance_usd is not None:
            balance_cny = balance_usd * rate

        now_ts = time.time()
        self._state["last_check_ts"] = now_ts
        self._state["last_balance_usd"] = balance_usd
        self._state["last_balance_cny"] = balance_cny

        if balance_usd is None:
            self._state["last_status"] = "error"
            self._state["last_status_msg"] = err_text or "未知错误"
            result["reason"] = self._state["last_status_msg"]
            result["error"] = True
            logger.warning(f"[DeepSeek 余额] 查询失败: {err_text}")
            # 查询失败也尝试提醒管理员
            await self._maybe_alert(
                balance_usd=None, balance_cny=None, error=self._state["last_status_msg"]
            )
            # 同时检查 LLM 是否也因余额耗尽无法使用
            await self._check_llm_depleted_and_warn()
        else:
            result["ok"] = True
            result["balance_usd"] = balance_usd
            result["balance_cny"] = balance_cny
            threshold_cny = self._get_threshold_cny()
            self._state["last_status"] = "low" if balance_cny <= threshold_cny else "ok"
            self._state["last_status_msg"] = (
                f"当前余额 ${balance_usd:.2f} (¥{balance_cny:.2f}) "
                f"（阈值 ¥{threshold_cny:.2f}，汇率 {rate:.2f}）"
            )
            logger.info(self._state["last_status_msg"])
            if balance_cny <= threshold_cny:
                await self._maybe_alert(
                    balance_usd=balance_usd,
                    balance_cny=balance_cny,
                    error=None,
                )
                # 低余额时顺便检测 LLM 是否可用
                await self._check_llm_depleted_and_warn()
            elif self.config.get("alert_on_query", False):
                await self._maybe_alert(
                    balance_usd=balance_usd,
                    balance_cny=balance_cny,
                    error=None,
                    force=True,
                )

        self._save_state()
        return result

    async def _http_get(
        self, endpoint: str, api_key: str, timeout: float
    ) -> Optional[dict[str, Any]]:
        """调用 DeepSeek 余额接口。

        优先使用 aiohttp（若可用），否则回退到线程内同步 urllib。
        """
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
        if aiohttp is not None:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
                async with session.get(endpoint, headers=headers) as resp:
                    text = await resp.text()
                    try:
                        return json.loads(text) if text else None
                    except json.JSONDecodeError:
                        return {"_raw": text}
        return await asyncio.to_thread(
            self._http_get_sync, endpoint, headers, timeout
        )

    @staticmethod
    def _http_get_sync(
        endpoint: str, headers: dict[str, str], timeout: float
    ) -> Optional[dict[str, Any]]:
        import urllib.request
        import urllib.error

        req = urllib.request.Request(endpoint, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(body) if body else None
                except json.JSONDecodeError:
                    return {"_raw": body}
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return {"_http_error": e.code, "_body": body}

    @staticmethod
    def _extract_balance(payload: dict[str, Any]) -> Optional[float]:
        """从常见 DeepSeek 余额字段中提取可用余额（美元）。"""
        if not isinstance(payload, dict):
            return None
        candidates = [
            payload,
            payload.get("data"),
            payload.get("balance"),
        ]
        keys = (
            "total_balance",
            "available_balance",
            "available",
            "balance",
            "usd_balance",
            "remain",
            "credits",
        )
        for c in candidates:
            if not isinstance(c, dict):
                continue
            for k in keys:
                if k in c:
                    try:
                        v = float(c[k])
                        return v
                    except (TypeError, ValueError):
                        pass
        return None

    @staticmethod
    def _extract_error(payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            return ""
        for k in ("message", "error", "error_message", "msg"):
            if k in payload:
                v = payload[k]
                if isinstance(v, str) and v.strip():
                    return v.strip()
        if "_http_error" in payload:
            return f"HTTP {payload['_http_error']}"
        return ""

    # ------------------------------------------------------------------ LLM 耗尽检测

    async def _check_llm_depleted_and_warn(self) -> None:
        """检测 LLM 是否因余额耗尽无法使用，若是则向管理员告警。"""
        try:
            use_persona = bool(self.config.get("use_persona", True))
        except Exception:
            use_persona = True
        if not use_persona:
            return  # 不使用 LLM 人格，则无需检测

        depleted, reason = await self._detect_llm_depleted()
        if depleted:
            logger.warning(
                f"[DeepSeek 余额] LLM 疑似因余额耗尽无法使用: {reason}"
            )
            await self._warn_llm_depleted(reason)

    async def _detect_llm_depleted(self) -> tuple[bool, str]:
        """通过一次最小化的 LLM 调用检测是否因余额耗尽而失败。

        返回 (是否耗尽, 原因描述)。
        """
        persona_provider_id = (
            self.config.get("persona_provider_id") or ""
        ).strip()
        provider_id = persona_provider_id
        umo = "deepseek_balance_plugin:check"

        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                return False, ""

        if not provider_id:
            return False, ""

        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt="ping",
                system_prompt="ping",
            )
            # 调用成功，说明 LLM 可用
            return False, ""
        except Exception as e:
            err_msg = str(e).lower()
            # 判断是否为余额耗尽相关错误
            depleted_keywords = (
                "insufficient",
                "balance",
                "depleted",
                "out of",
                "no balance",
                "quota",
                "402",
                "payment required",
                "billing",
                "credit",
                "余额",
                "配额",
            )
            for kw in depleted_keywords:
                if kw in err_msg:
                    return True, str(e)[:200]
            # 其他错误不判定为耗尽
            return False, str(e)[:200]

    async def _warn_llm_depleted(self, reason: str) -> None:
        """发送 LLM 耗尽告警，带冷却。"""
        try:
            cooldown = int(
                self.config.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
                or DEFAULT_COOLDOWN_MINUTES
            )
        except (TypeError, ValueError):
            cooldown = DEFAULT_COOLDOWN_MINUTES

        now = time.time()
        last_depleted_ts = float(
            self._state.get("last_depleted_warn_ts", 0.0) or 0.0
        )
        if cooldown > 0 and now - last_depleted_ts < cooldown * 60:
            logger.info("[DeepSeek 余额] LLM 耗尽告警冷却期内，跳过。")
            return

        template = (
            self.config.get("depleted_message") or DEFAULT_DEPLETED_MESSAGE
        )
        provider_name = (
            self._state.get("provider_name")
            or self._state.get("provider_id")
            or "DeepSeek"
        )
        message = template.replace("{provider}", str(provider_name))
        if reason:
            message += f"\n\n原因：{reason}"

        notified = await self._dispatch_notifications(message)
        if notified:
            self._state["last_depleted_warn_ts"] = now
        else:
            self._pending_alerts.append(message)
            if len(self._pending_alerts) > 20:
                self._pending_alerts = self._pending_alerts[-20:]
            logger.warning("[DeepSeek 余额] LLM 耗尽告警未送达，已入队。")

    # ------------------------------------------------------------------ 告警

    async def _maybe_alert(
        self,
        balance_usd: Optional[float],
        balance_cny: Optional[float],
        error: Optional[str],
        force: bool = False,
    ) -> None:
        """向配置的管理群 / 管理员发送告警。包含冷却控制。"""
        try:
            cooldown = int(
                self.config.get("cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)
                or DEFAULT_COOLDOWN_MINUTES
            )
        except (TypeError, ValueError):
            cooldown = DEFAULT_COOLDOWN_MINUTES

        now = time.time()
        if balance_cny is not None:
            bucket = f"balance_low:{round(balance_cny, 2)}"
        else:
            bucket = f"error:{error or 'unknown'}"
        last_ts = float(self._state.get("last_alert_ts", 0.0) or 0.0)
        last_bucket = self._state.get("last_alert_bucket", "")
        if (
            not force
            and bucket == last_bucket
            and cooldown > 0
            and now - last_ts < cooldown * 60
        ):
            logger.info("[DeepSeek 余额] 告警冷却期内，跳过本次通知。")
            return

        rate = self._get_usd_to_cny_rate()
        threshold_cny = self._get_threshold_cny()
        provider_display = (
            self._state.get("provider_name") or self._state.get("provider_id") or "-"
        )

        if error:
            title = "⚠️ DeepSeek 余额查询失败"
            body = (
                f"原因：{error}\n"
                f"请检查 AstrBot 配置文件中的 DeepSeek API Key 是否正确，"
                f"或网络是否可以访问 {self.config.get('api_endpoint') or DEFAULT_ENDPOINT}。"
            )
            text = (
                f"{title}\n"
                f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Provider：{provider_display}\n\n"
                f"{body}"
            )
        else:
            # 构建余额不足告警
            text = await self._build_low_balance_text(
                balance_usd, balance_cny, rate, threshold_cny, provider_display
            )

        notified = await self._dispatch_notifications(text)
        if notified:
            self._state["last_alert_ts"] = now
            self._state["last_alert_bucket"] = bucket
        else:
            self._pending_alerts.append(text)
            if len(self._pending_alerts) > 20:
                self._pending_alerts = self._pending_alerts[-20:]
            logger.warning("[DeepSeek 余额] 未找到可用的通知目标，告警已入队等待下次事件。")

    async def _build_low_balance_text(
        self,
        balance_usd: Optional[float],
        balance_cny: Optional[float],
        rate: float,
        threshold_cny: float,
        provider_display: str,
    ) -> str:
        """构建低余额告警文案，优先使用 LLM 人格生成。"""
        try:
            use_persona = bool(self.config.get("use_persona", True))
        except Exception:
            use_persona = True

        if use_persona:
            persona_text = await self._generate_persona_alert(
                balance_usd, balance_cny, rate, threshold_cny, provider_display
            )
            if persona_text:
                return persona_text

        # 回退：固定模板
        return self._build_template_alert(
            balance_usd, balance_cny, rate, threshold_cny, provider_display
        )

    def _build_template_alert(
        self,
        balance_usd: Optional[float],
        balance_cny: Optional[float],
        rate: float,
        threshold_cny: float,
        provider_display: str,
    ) -> str:
        """固定模板告警文案（LLM 不可用时兜底）。"""
        usd_str = f"${balance_usd:.2f}" if balance_usd is not None else "未知"
        cny_str = f"¥{balance_cny:.2f}" if balance_cny is not None else "未知"
        return (
            f"⚠️ DeepSeek 账户余额不足\n"
            f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Provider：{provider_display}\n\n"
            f"当前可用余额：{usd_str}（{cny_str}）\n"
            f"告警阈值：¥{threshold_cny:.2f}（汇率 {rate:.2f}）\n"
            f"请尽快前往 DeepSeek 控制台充值，以免影响服务。"
        )

    async def _generate_persona_alert(
        self,
        balance_usd: Optional[float],
        balance_cny: Optional[float],
        rate: float,
        threshold_cny: float,
        provider_display: str,
    ) -> str:
        """调用 LLM 以当前人格生成告警文案。失败时返回空串。"""
        try:
            use_persona = bool(self.config.get("use_persona", True))
        except Exception:
            use_persona = True
        if not use_persona:
            return ""

        try:
            persona_provider_id = (
                self.config.get("persona_provider_id") or ""
            ).strip()
            provider_id = persona_provider_id
            umo = "deepseek_balance_plugin:alert"
            if not provider_id:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            if not provider_id:
                logger.warning("[DeepSeek 余额] 无可用 LLM Provider，跳过人格告警。")
                return ""

            try:
                persona_prompt = await self._get_persona_prompt(umo)
            except Exception as e:
                logger.warning(f"[DeepSeek 余额] 获取人格失败: {e}")
                persona_prompt = ""

            usd_str = f"${balance_usd:.2f}" if balance_usd is not None else "未知"
            cny_str = f"¥{balance_cny:.2f}" if balance_cny is not None else "未知"

            system_prompt = persona_prompt.rstrip() + (
                "\n\n[附加任务] 你现在需要以你的人格设定和语气，"
                "向一名系统管理员发送一条关于 DeepSeek API 账户余额不足的告警通知。"
                "要求：简短、严肃但符合你人设的语气，不超过 150 字。"
                "只输出通知正文，不要输出引号、JSON 或任何解释。"
            )
            user_prompt = (
                f"Provider：{provider_display}\n"
                f"当前余额：{usd_str}（约 {cny_str}）\n"
                f"告警阈值：¥{threshold_cny:.2f}（汇率 {rate:.2f}）\n"
                f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"请生成一条向管理员告警的通知文案。"
            )

            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=user_prompt,
                system_prompt=system_prompt,
            )
            text = (getattr(resp, "completion_text", "") or "").strip().strip('"').strip()
            if text:
                # 追加元信息（时间/Provider）在后面，不干扰人格正文
                header = (
                    f"⚠️ DeepSeek 余额告警\n"
                    f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"Provider：{provider_display}\n\n"
                )
                footer = (
                    f"\n\n当前余额：{usd_str}（{cny_str}）  阈值：¥{threshold_cny:.2f}"
                )
                return header + text + footer
            return ""
        except Exception as e:
            logger.warning(f"[DeepSeek 余额] 人格告警文案生成失败，回退模板: {e}")
            return ""

    async def _get_persona_prompt(self, umo: str) -> str:
        """获取当前首选人格的 system prompt。"""
        pm = getattr(self.context, "persona_manager", None)
        if pm is None:
            return ""
        try:
            persona = await pm.get_default_persona_v3(umo=umo)
        except Exception as e:
            logger.warning(f"[DeepSeek 余额] get_default_persona_v3 失败: {e}")
            persona = None
        if persona is None:
            persona = getattr(pm, "selected_default_persona_v3", None)
        if persona is None:
            return ""
        if isinstance(persona, dict):
            return persona.get("prompt", "") or ""
        return getattr(persona, "prompt", "") or ""

    # ------------------------------------------------------------------ 通知发送

    async def _dispatch_notifications(self, text: str) -> bool:
        """向配置的群 / 用户发送告警。返回是否至少送达一处。"""
        notified_any = False
        targets = self._parse_targets()
        for kind, platform_id, target_id in targets:
            ok = await self._send_proactive(kind, platform_id, target_id, text)
            if ok:
                notified_any = True
        return notified_any

    def _parse_targets(self) -> list[tuple[str, str, str]]:
        targets: list[tuple[str, str, str]] = []
        for raw in self.config.get("notify_groups") or []:
            t = self._parse_one_target("group", raw)
            if t:
                targets.append(t)
        for raw in self.config.get("notify_users") or []:
            t = self._parse_one_target("private", raw)
            if t:
                targets.append(t)
        return targets

    @staticmethod
    def _parse_one_target(
        kind: str, raw: Any
    ) -> Optional[tuple[str, str, str]]:
        if not isinstance(raw, str):
            return None
        s = raw.strip()
        if not s:
            return None
        if ":" in s:
            platform_id, _, target_id = s.partition(":")
            platform_id = platform_id.strip()
            target_id = target_id.strip()
            if platform_id and target_id:
                return (kind, platform_id, target_id)
        return (kind, "", s)

    async def _send_proactive(
        self, kind: str, platform_id: str, target_id: str, text: str
    ) -> bool:
        """尽力通过 AstrBot 的 Context / Bot 发送一条主动消息。"""
        sent = await self._try_context_send(platform_id, target_id, kind, text)
        if sent:
            return True

        sent = await self._try_bot_send(platform_id, target_id, kind, text)
        if sent:
            return True

        logger.warning(
            f"[DeepSeek 余额] 告警未送达（kind={kind} platform={platform_id} target={target_id}）"
        )
        logger.info(f"[DeepSeek 余额] 告警内容：{text}")
        return False

    async def _try_context_send(
        self, platform_id: str, target_id: str, kind: str, text: str
    ) -> bool:
        ctx = self.context
        pid = platform_id or "unk"
        if kind == "group":
            umo = f"{pid}:unk:group:{target_id}"
        else:
            umo = f"{pid}:unk:private:{target_id}"

        candidates = [
            ("send_msg", (umo, text)),
            ("send_message", (umo, text)),
            ("broadcast_msg", (text,)),
        ]
        for name, args in candidates:
            fn = getattr(ctx, name, None)
            if callable(fn):
                try:
                    result = fn(*args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    logger.info(
                        f"[DeepSeek 余额] 通过 context.{name} 已发送告警"
                    )
                    return True
                except Exception as e:
                    logger.debug(f"[DeepSeek 余额] context.{name} 失败: {e}")
        return False

    async def _try_bot_send(
        self, platform_id: str, target_id: str, kind: str, text: str
    ) -> bool:
        ctx = self.context
        bot_attrs = ("bot", "bots", "platform_bots", "bot_registry")
        bots: list[Any] = []
        for attr in bot_attrs:
            v = getattr(ctx, attr, None)
            if v is None:
                continue
            if isinstance(v, dict):
                for key, b in v.items():
                    if not platform_id or str(key) == platform_id:
                        bots.append(b)
            elif isinstance(v, (list, tuple)):
                for b in v:
                    bots.append(b)
            else:
                bots.append(v)

        if not bots:
            return False

        for bot in bots:
            client = getattr(bot, "api", None)
            if client is None:
                send_fn = (
                    getattr(bot, "send_group_msg", None)
                    if kind == "group"
                    else getattr(bot, "send_private_msg", None)
                )
                if callable(send_fn):
                    try:
                        result = send_fn(
                            group_id=int(target_id) if kind == "group" else None,
                            user_id=int(target_id) if kind != "group" else None,
                            message=text,
                        )
                        if asyncio.iscoroutine(result):
                            await result
                        return True
                    except Exception as e:
                        logger.debug(f"[DeepSeek 余额] bot 直接发送失败: {e}")
                continue

            try:
                if kind == "group":
                    await client.call_action(
                        "send_group_msg",
                        group_id=int(target_id),
                        message=text,
                    )
                else:
                    await client.call_action(
                        "send_private_msg",
                        user_id=int(target_id),
                        message=text,
                    )
                return True
            except Exception as e:
                logger.debug(
                    f"[DeepSeek 余额] OneBot 动作发送失败 platform={platform_id}: {e}"
                )
        return False

    # ------------------------------------------------------------------ 指令

    @filter.command(
        command_name="deepseek_balance",
        description="查询当前 DeepSeek Provider 的账户余额（管理员）",
        permission_type="admin",
    )
    async def on_command_deepseek_balance(self, event: AstrMessageEvent):
        async for r in self._handle_manual(event, trigger="command"):
            yield r

    @filter.command(
        command_name="余额",
        description="查询 DeepSeek 余额（管理员）",
        permission_type="admin",
    )
    async def on_command_balance(self, event: AstrMessageEvent):
        async for r in self._handle_manual(event, trigger="command"):
            yield r

    async def _handle_manual(self, event: AstrMessageEvent, trigger: str):
        yield event.make_result().message("正在查询 DeepSeek 余额…").send()
        try:
            r = await self._do_check(trigger=trigger)
        except Exception as e:
            logger.warning(f"[DeepSeek 余额] 手动查询异常: {e}")
            yield event.make_result().message(f"查询异常：{e}").send()
            return

        pending = self._flush_pending_alerts()
        rate = self._get_usd_to_cny_rate()
        threshold_cny = self._get_threshold_cny()

        if r.get("ok"):
            balance_usd = r.get("balance_usd")
            balance_cny = r.get("balance_cny")
            low = balance_cny is not None and balance_cny <= threshold_cny
            header = "⚠️ 余额不足" if low else "✅ 余额正常"
            usd_str = f"${balance_usd:.2f}" if balance_usd is not None else "未知"
            cny_str = f"¥{balance_cny:.2f}" if balance_cny is not None else "未知"
            text = (
                f"{header}\n"
                f"Provider：{self._state.get('provider_name') or self._state.get('provider_id') or '-'}\n"
                f"当前余额：{usd_str}（{cny_str}）\n"
                f"告警阈值：¥{threshold_cny:.2f}（汇率 {rate:.2f}）\n"
                f"查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
        elif r.get("reason") == "no_provider":
            text = (
                "ℹ️ 未在 AstrBot 配置中发现 DeepSeek Provider，"
                "请先在 WebUI「模型提供商」中添加并启用 DeepSeek Provider。"
            )
        else:
            text = f"❌ 查询失败：{r.get('reason') or '未知错误'}"
        if pending:
            text += "\n\n（已补发积压告警）"
        yield event.make_result().message(text).send()

    # ------------------------------------------------------------------ 兜底发送

    @filter.event_message_type(filter.EventMessageType.ALL, priority=9999)
    async def _on_any_message_flush(self, event: AstrMessageEvent):
        if not self._pending_alerts:
            return
        text = self._pending_alerts.pop(0)
        logger.info("[DeepSeek 余额] 利用当前事件兜底补发告警。")
        yield event.make_result().message(text).send()

    def _flush_pending_alerts(self) -> list[str]:
        out = list(self._pending_alerts)
        self._pending_alerts.clear()
        return out

    # ------------------------------------------------------------------ 状态持久化

    def _load_state(self) -> None:
        if not os.path.exists(self._data_path):
            return
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._state.update(data)
        except Exception as e:
            logger.warning(f"[DeepSeek 余额] 状态文件读取失败: {e}")

    def _save_state(self) -> None:
        try:
            st = {
                "last_check_ts": self._state.get("last_check_ts", 0.0),
                "last_balance_usd": self._state.get("last_balance_usd"),
                "last_balance_cny": self._state.get("last_balance_cny"),
                "last_status": self._state.get("last_status", "init"),
                "last_status_msg": self._state.get("last_status_msg", ""),
                "last_alert_ts": self._state.get("last_alert_ts", 0.0),
                "last_alert_bucket": self._state.get("last_alert_bucket", ""),
                "last_depleted_warn_ts": self._state.get("last_depleted_warn_ts", 0.0),
                "provider_id": self._state.get("provider_id", ""),
                "provider_name": self._state.get("provider_name", ""),
            }
            with open(self._data_path, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[DeepSeek 余额] 状态保存失败: {e}")
