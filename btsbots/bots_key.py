import gc
import os
import sys
import termios
import subprocess
import multiprocessing
from functools import wraps
from binascii import hexlify
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from btsbots.graphene_light import PrivateKey

ctx = multiprocessing.get_context('fork')

def sandbox_execute(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self._encrypted_key_store:
            raise ValueError("密钥库为空，请先导入 WIF 私钥。")

        parent_conn, child_conn = ctx.Pipe()

        def _child_worker():
            decrypted_store = {}
            try:
                # 在子进程安全沙盒中解密已保存的 WIF 密钥
                for pub_addr, cipher in self._encrypted_key_store.items():
                    plain_bytes = self._decrypt(cipher)
                    decrypted_store[pub_addr] = plain_bytes

                result = func(self, decrypted_store, *args, **kwargs)
                child_conn.send({'status': 'success', 'result': result})
            except Exception as e:
                child_conn.send({'status': 'error', 'message': str(e)})
            finally:
                # 内存抹零保护
                for k, v in decrypted_store.items():
                    if isinstance(v, bytearray):
                        for i in range(len(v)):
                            v[i] = 0
                child_conn.close()
                os._exit(0)

        try:
            process = ctx.Process(target=_child_worker)
            process.start()
            response = parent_conn.recv()
            process.join()

            if response.get('status') == 'error':
                raise RuntimeError(f"沙盒执行异常 [{func.__name__}]: {response.get('message')}")

            return response.get('result')
        finally:
            parent_conn.close()

    return wrapper

class BotsKey:
    """
    智能多 Key 密钥管理器：
    以标准 BitShares 公钥地址（如 BTS...）为索引，常驻内存仅保留 AES 密文。
    所有操作强制指定目标公钥。
    """
    def __init__(self):
        self._memory_master_key = os.urandom(32)
        # 结构：{ pubkey_str: encrypted_bytes }
        self._encrypted_key_store = {}

    @staticmethod
    def secure_clear(data):
        if isinstance(data, bytearray) and data:
            for i in range(len(data)):
                data[i] = 0

    def _encrypt(self, plaintext_bytes: bytes) -> bytes:
        iv = os.urandom(16)
        cipher = AES.new(self._memory_master_key, AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(pad(plaintext_bytes, AES.block_size))
        return iv + ciphertext

    def _decrypt(self, ciphertext_bytes: bytes) -> bytearray:
        iv = ciphertext_bytes[:16]
        ciphertext = ciphertext_bytes[16:]
        cipher = AES.new(self._memory_master_key, AES.MODE_CBC, iv)
        decrypted = unpad(cipher.decrypt(ciphertext), AES.block_size)
        return bytearray(decrypted)

    def has_key(self, pub_key: str) -> bool:
        """检查指定公钥对应的私钥是否已加载"""
        if not pub_key:
            return False
        return str(pub_key).strip() in self._encrypted_key_store

    def get_available_pubkeys(self) -> list[str]:
        """获取当前已成功导入的所有公钥列表"""
        return list(self._encrypted_key_store.keys())

    def add_key_by_wif(self, wif_str: str) -> str:
        """解析 WIF 并在本地字典建立以公钥地址为索引的映射"""
        wif_clean = wif_str.strip()
        if not wif_clean:
            return None
        pKey = PrivateKey.from_wif(wif_clean)
        pub_addr = pKey.get_public_key()
        encrypted = self._encrypt(wif_clean.encode('utf-8'))
        self._encrypted_key_store[pub_addr] = encrypted
        return pub_addr

    def ingest_from_stdin(self):
        """手动循环输入多把 Key，直到用户输入空行结束"""
        self.clear_current_key()
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)

        new_settings = termios.tcgetattr(fd)
        new_settings[3] = new_settings[3] & ~termios.ECHO & ~termios.ICANON
        termios.tcsetattr(fd, termios.TCSANOW, new_settings)

        idx = 1
        try:
            while True:
                print(f"请输入第 {idx} 把 WIF Key (直接回车结束输入): ", end="", flush=True)
                temp_buffer = bytearray()
                while True:
                    char_byte = sys.stdin.buffer.read(1)
                    if char_byte == b'\n' or char_byte == b'\r':
                        break
                    temp_buffer.extend(char_byte)

                wif_str = temp_buffer.decode('utf-8').strip()
                self.secure_clear(temp_buffer)
                print()

                if not wif_str:
                    break

                pub = self.add_key_by_wif(wif_str)
                print(f"  -> 成功载入 Key，对应公钥: {pub}")
                idx += 1
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, old_settings)
        return self

    def ingest_from_pass(self, secret_path: str) -> str:
        """从 Unix pass 自动智能识别所有行中的 WIF 格式私钥"""
        self.clear_current_key()
        result = subprocess.run(
            ["pass", secret_path],
            capture_output=True,
            text=True,
            check=True,
        )
        try:
            lines = result.stdout.strip().split("\n")
            if not lines:
                raise ValueError("pass 数据为空")

            account = lines[0].strip()
            loaded_count = 0

            for line in lines[1:]:
                clean_line = line.strip()
                if ":" in clean_line:
                    clean_line = clean_line.split(":")[-1].strip()

                if clean_line.startswith("5") and len(clean_line) >= 50:
                    self.add_key_by_wif(clean_line)
                    loaded_count += 1

            print(f"✓ [BotsKey] 从 pass 成功载入账号 [{account}] 及其关联的 {loaded_count} 把 Key。")
            return account
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.strip() if e.stderr else "未知进程错误"
            raise RuntimeError(f"Pass 密码管理器调用失败: {error_msg}")
        finally:
            del result
            gc.collect()

    def ingest_from_file(self, filepath: str) -> str:
        """从本地凭证文件（如 credentials.txt）提取所有 WIF 密钥"""
        self.clear_current_key()
        with open(filepath, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]

        if not lines:
            raise ValueError("凭证文件为空")

        account = lines[0]
        loaded_count = 0

        for line in lines[1:]:
            clean_line = line.strip()
            if ":" in clean_line:
                clean_line = clean_line.split(":")[-1].strip()

            if clean_line.startswith("5") and len(clean_line) >= 50:
                self.add_key_by_wif(clean_line)
                loaded_count += 1

        print(f"✓ [BotsKey] 从文件成功载入账号 [{account}] 及其关联的 {loaded_count} 把 Key。")
        return account

    def clear_current_key(self):
        self._encrypted_key_store.clear()
        self._memory_master_key = os.urandom(32)

    def __del__(self):
        self.clear_current_key()

    @sandbox_execute
    def sign_message(self, decrypted_store: dict, pub_key: str, message_str: str) -> dict:
        """使用指定的公钥对文本消息进行签名"""
        clean_pub = str(pub_key).strip()
        if clean_pub not in decrypted_store:
            raise KeyError(f"密钥管理器中未找到指定公钥 [{clean_pub}] 对应的私钥")

        wif_bytes = decrypted_store[clean_pub]
        pKey = PrivateKey.from_wif(wif_bytes.decode('utf-8'))
        sig_bytes = pKey.sign_message(message_str)

        return {
            "data": message_str,
            "pubkey": clean_pub,
            "signature": hexlify(sig_bytes).decode('ascii')
        }

    def verify_message(self, payload: dict) -> bool:
        from btsbots.graphene_light import verify_message as bts_verify_message
        from binascii import unhexlify
        return bts_verify_message(
            payload["data"], unhexlify(payload["signature"]), payload["pubkey"]
        )

    @sandbox_execute
    def sign_transaction(self, decrypted_store: dict, pub_key: str, payload: dict) -> dict:
        """使用指定的公钥对 BitShares 交易载荷进行序列化签名"""
        import btsbots.custom_tx as transactions
        from binascii import unhexlify, hexlify

        clean_pub = str(pub_key).strip()
        if clean_pub not in decrypted_store:
            raise KeyError(f"密钥管理器中未找到指定公钥 [{clean_pub}] 对应的私钥，无法签署交易")

        pKey = PrivateKey.from_wif(decrypted_store[clean_pub].decode('utf-8'))

        transaction = transactions.New_Signed_Transaction(
            ref_block_num=payload["ref_block_num"],
            ref_block_prefix=payload["ref_block_prefix"],
            expiration=payload["expiration"],
            operations=payload["operations"]
        )
        final_payload = transaction.json()
        serialized_tx_bytes = bytes(transaction)[:-1]
        BTS_CHAIN_ID_HEX = "4018d7844c78f6a6c41c6a552b898022310fc5dec06da467ee7905a8dad512c8"
        signing_message = unhexlify(BTS_CHAIN_ID_HEX) + serialized_tx_bytes

        custom_sig_bytes = pKey.sign_message(signing_message)
        custom_sig_hex = hexlify(custom_sig_bytes).decode('utf-8')
        final_payload["signatures"] = [custom_sig_hex]
        return final_payload

    @sandbox_execute
    def encrypt_memo(self, decrypted_store: dict, my_pub_key: str, to_pub_key: str, message: str) -> dict:
        """使用指定的发起方公钥和接收方公钥对 Memo 内容进行 Diffie-Hellman 加密"""
        from graphenebase.account import PrivateKey as GPrivateKey, PublicKey as GPublicKey
        from graphenebase.memo import encode_memo
        import random

        clean_my_pub = str(my_pub_key).strip()
        if clean_my_pub not in decrypted_store:
            raise KeyError(f"密钥管理器中未找到发件方公钥 [{clean_my_pub}] 对应的私钥，无法加密 Memo")

        target_wif = decrypted_store[clean_my_pub].decode('utf-8')
        priv_key = GPrivateKey(target_wif, prefix="BTS")
        to_pub_obj = GPublicKey(to_pub_key, prefix="BTS")

        nonce_int = random.randint(100000000000000, 9007199254740991)
        encrypted_hex = encode_memo(priv_key, to_pub_obj, nonce_int, message)

        return {
            "from": str(priv_key.pubkey),
            "to": str(to_pub_obj),
            "nonce": str(nonce_int),
            "message": encrypted_hex
        }

    @sandbox_execute
    def decrypt_memo(self, decrypted_store: dict, my_pub_key: str, memo_info: dict) -> str:
        """使用已确认属于本机的公钥对链上 Memo 信息进行解密"""
        from graphenebase.account import PrivateKey as GPrivateKey, PublicKey as GPublicKey
        from graphenebase.memo import decode_memo

        clean_my_pub = str(my_pub_key).strip()
        if clean_my_pub not in decrypted_store:
            raise KeyError(f"密钥管理器中未找到指定解密公钥 [{clean_my_pub}] 对应的私钥")

        keys = memo_info.get('k', [])
        if not keys or len(keys) < 2:
            raise ValueError("Memo 数据缺少通信双方公钥对")

        # 对方公钥为 keys 中非当前解密公钥的另一个
        other_pub_str = keys[1] if clean_my_pub == keys[0] else keys[0]
        other_pub_obj = GPublicKey(other_pub_str, prefix="BTS")

        target_wif = decrypted_store[clean_my_pub].decode('utf-8')
        priv_key = GPrivateKey(target_wif, prefix="BTS")
        nonce_int = int(memo_info["n"])

        plaintext = decode_memo(priv_key, other_pub_obj, nonce_int, memo_info["m"])
        return plaintext
