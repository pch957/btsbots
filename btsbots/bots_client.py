from typing import Optional, Tuple, Any
import json
import sqlite3
import sys

from btsbots.meteor_client import MeteorDDPClient
from btsbots.bots_key import BotsKey

class BotsClient(MeteorDDPClient):
    def __init__(self, db_path: str = "bots.sqlite"):
        super().__init__()
        self.db_path = db_path
        self._init_db_cache()
        self.account_name: Optional[str] = None
        self.user_id: Optional[str] = None
        self.bts_id: Optional[str] = None

        # 统一维持一个智能多 Key 密钥管理器
        self.key_manager = BotsKey()
        self.description = "BTSBots client"

    def extend_arguments(self, parser):
        parser.add_argument(
            "--pass",
            dest="bypass_auth",
            type=str,
            help="Bypass interactive credentials using pass entry",
        )
        parser.add_argument(
            "--keyfile",
            type=str,
            help="Load credentials directly from a local generated account credential file",
        )

    async def run(self):
        await super().run()
        args = self.args
        try:
            await self._login(args.bypass_auth, args.keyfile)
        except Exception as err:
            print(f"登录失败: {err}")
            await self.close()
            raise

    async def _login(self, passPath="", keyfile=""):
        """使用统一密钥管理器执行去中心化登录"""
        if keyfile:
            print(f"[*] 正在从本地凭证文件加载账号与多重密钥: {keyfile}")
            self.account_name = self.key_manager.ingest_from_file(keyfile)
        elif passPath:
            self.account_name = self.key_manager.ingest_from_pass(passPath)
        else:
            print("请输入用户名: ", end="", flush=True)
            self.account_name = sys.stdin.readline().strip()
            print("请依次输入您的 WIF Keys (支持输入多把，输入空行结束):")
            self.key_manager.ingest_from_stdin()

        # 1. 登录前必须根据账号文档确定链上 Active Key
        active_pub = await self._resolve_account_active_pubkey(self.account_name)

        auth_payload = self._generate_auth_payload(self.account_name, active_pub, site='btsbots.com')
        login_res = await self.call("login", {"btsWallet": auth_payload})
        self.user_id = login_res.get("id")
        _, self.bts_id = await self.get_account_brief(self.account_name)
        print(f"✓ [BotsClient] 登录成功！")

    async def _resolve_account_active_pubkey(self, account_name: str) -> str:
        """从链上获取账号的 Active Key 并在本地密钥库中核对存在性"""
        account_doc = await self.get_account_info(account_name)
        active_keys = account_doc.get("k", {}).get("a", [])
        if not active_keys:
            raise ValueError(f"链上账号 [{account_name}] 未配置任何有效 Active Key。")

        for key in active_keys:
            if self.key_manager.has_key(key):
                return key

        available_keys = self.key_manager.get_available_pubkeys()
        raise KeyError(
            f"账号 [{account_name}] 的链上 Active Key {active_keys} 不存在于当前已导入的私钥库中！"
            f"(本地持有的公钥为: {available_keys})"
        )

    def _generate_auth_payload(self, account_name: str, active_pubkey: str, site: str="btsbots.com", token: str="", ip: str="") -> dict:
        import time
        import json

        auth_data = {
            "username": account_name,
            "site": site,
            "ip": ip,
            "token": token,
            "time": int(time.time())
        }
        message_str = json.dumps(auth_data, sort_keys=True)
        # 显式使用经过校验的 Active Key 签名
        payload = self.key_manager.sign_message(active_pubkey, message_str)
        return {
            "user": account_name,
            "verify": payload
        }

    async def request_otp(self) -> str:
        """【接口】为当前登录会话申请一个 6 位数字的网页前端一次性登录码 (OTP)"""
        if not self.user_id:
            raise PermissionError("未登录会话，无法申请网页 2FA 令牌")

        print("[BotsClient] 正在向 DDP 核心申请临时 Web OTP 验证码...")
        otp_token = await self.call("generateWebOtp")
        return str(otp_token)

    async def login_with_otp(self, account_name: str, otp_token: str) -> dict:
        """【接口】模拟普通用户前端：凭一次性 OTP 令牌完成会话认证"""
        self.account_name = account_name
        print(f"[BotsClient] 正在尝试提交临时 OTP 凭证锁定 Web 会话...")

        login_msg = {
            "otp": {
                "account": account_name,
                "token": otp_token
            }
        }
        login_res = await self.call("login", login_msg)
        self.user_id = login_res.get("id")
        _, self.bts_id = await self.get_account_brief(account_name)
        print(f"✓ [BotsClient] 用户登录成功（通过OTP）。分配的ID: {self.user_id}")
        return login_res

    def _init_db_cache(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cached_assets (
                    symbol TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, raw_data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cached_accounts (
                    symbol TEXT PRIMARY KEY, id TEXT NOT NULL UNIQUE, raw_data TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cached_memo (
                    id INTEGER PRIMARY KEY, raw_data TEXT NOT NULL
                )
            """)
            conn.commit()

    def _get_account_local(self, account_symbol_or_id: str) -> dict:
        is_id = account_symbol_or_id.startswith("1.2.")
        query_field = "id" if is_id else "symbol"
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(f"SELECT raw_data FROM cached_accounts WHERE {query_field} = ?", (account_symbol_or_id,)).fetchone()
            if row: return json.loads(row[0])
        return None

    async def _get_account_remote(self, account_symbol_or_id: str) -> dict:
        is_id = account_symbol_or_id.startswith("1.2.")
        if is_id:
            raw_num = int(account_symbol_or_id.split(".")[-1])
            meteor_doc = await self.call("get_account_document_by_id", raw_num)
        else:
            meteor_doc = await self.call("get_account_document_by_symbol", account_symbol_or_id)
        if meteor_doc:
            _symbol = str(meteor_doc['u']).strip()
            _id = f"1.2.{meteor_doc['_id']}"
            meteor_doc['_id'] = _id
            return meteor_doc
        return None

    async def get_account_info(self, account_symbol_or_id: str) -> dict:
        info = self._get_account_local(account_symbol_or_id)
        if info: return info
        info = await self._get_account_remote(account_symbol_or_id)
        if info:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("INSERT OR REPLACE INTO cached_accounts VALUES (?, ?, ?)", (info['u'], info['_id'], json.dumps(info)))
            return info
        raise ValueError(f"无法获取账号信息: {account_symbol_or_id}")

    async def get_account_brief(self, account_symbol_or_id: str) -> Tuple[str, str]:
        info = await self.get_account_info(account_symbol_or_id)
        if info: return info['u'], info['_id']
        return None, None

    def _get_asset_local(self, asset_symbol_or_id: str) -> dict:
        is_id = asset_symbol_or_id.startswith("1.3.")
        query_field = "id" if is_id else "symbol"
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(f"SELECT raw_data FROM cached_assets WHERE {query_field} = ?", (asset_symbol_or_id,)).fetchone()
            if row: return json.loads(row[0])
        return None

    async def _get_asset_remote(self, asset_symbol_or_id: str) -> dict:
        is_id = asset_symbol_or_id.startswith("1.3.")
        if is_id:
            raw_num = int(asset_symbol_or_id.split(".")[-1])
            meteor_doc = await self.call("get_asset_document_by_id", raw_num)
        else:
            meteor_doc = await self.call("get_asset_document_by_symbol", asset_symbol_or_id)
        if meteor_doc:
            _symbol = str(meteor_doc['a']).upper().strip()
            _id = f"1.3.{meteor_doc['_id']}"
            meteor_doc['_id'] = _id
            return meteor_doc
        return None

    async def get_asset_info(self, asset_symbol_or_id: str) -> dict:
        info = self._get_asset_local(asset_symbol_or_id)
        if info: return info
        info = await self._get_asset_remote(asset_symbol_or_id)
        if info:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("INSERT OR REPLACE INTO cached_assets VALUES (?, ?, ?)", (info['a'], info['_id'], json.dumps(info)))
            return info
        raise ValueError(f"无法获取资产信息: {asset_symbol_or_id}")

    async def get_asset_brief(self, asset_symbol_or_id: str) -> Tuple[str, str, int]:
        info = await self.get_asset_info(asset_symbol_or_id)
        if info: return info['a'], info['_id'], info['p']
        return None, None, None
