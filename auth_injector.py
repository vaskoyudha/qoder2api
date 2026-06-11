#!/usr/bin/env python3
"""
Qodercli Auth Injector
Inject 9Router tokens into qodercli auth files to use free tier
"""

import json
import base64
import sqlite3
import uuid
from pathlib import Path
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

class QoderAuthInjector:
    def __init__(self, qoder_auth_dir=None):
        if qoder_auth_dir is None:
            qoder_auth_dir = Path.home() / ".qoder" / ".auth"
        self.auth_dir = Path(qoder_auth_dir)
        self.auth_dir.mkdir(parents=True, exist_ok=True)
    
    def get_9router_tokens(self, db_path=None):
        """Get all Qoder tokens from 9Router database"""
        if db_path is None:
            db_path = Path.home() / ".9router" / "db" / "data.sqlite"
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT id, name, data 
            FROM providerConnections 
            WHERE provider = 'qoder' AND isActive = 1
            ORDER BY priority
        """)
        
        tokens = []
        for row in cursor.fetchall():
            account_id, name, data_json = row
            data = json.loads(data_json)
            tokens.append({
                "id": account_id,
                "name": name,
                "accessToken": data.get("accessToken"),
                "refreshToken": data.get("refreshToken"),
                "userId": data.get("providerSpecificData", {}).get("userId"),
                "machineId": data.get("providerSpecificData", {}).get("machineId"),
                "organizationId": data.get("providerSpecificData", {}).get("organizationId", "")
            })
        
        conn.close()
        return tokens
    
    def create_user_data(self, token_info):
        """Create user data structure for qodercli"""
        # Current timestamp (seconds since epoch)
        current_time = 1749646933  # 2026-06-10
        
        user_data = {
            "uid": token_info["userId"],
            "name": token_info["name"],
            "security_oauth_token": token_info["accessToken"],
            "access_token": token_info["accessToken"],
            "refresh_token": token_info["refreshToken"],
            "expire_time": current_time + 30 * 24 * 3600,  # 30 days
            "refresh_token_expire_time": current_time + 365 * 24 * 3600,  # 1 year
            "login_method": "device",
            "login_timestamp": current_time,
            "encrypt_user_info": "",  # Not needed for device auth
            "key": "",  # Not needed for device auth
            "email": f"{token_info['name'].lower().replace(' ', '')}@example.com",
            "avatar_url": f"https://qoder.com/users/{token_info['userId']}/default/avatars",
            "data_policy_agreed": True
        }
        
        return user_data
    
    def encrypt_user_data(self, user_data, machine_id):
        """
        Encrypt user data for qodercli
        
        Encryption (reverse of LocalAuth decryption):
        - Algorithm: AES/CBC/PKCS5Padding
        - Key: machine_id[0:16]
        - IV: same as key
        - Output: Base64 encoded ciphertext
        """
        # Convert to JSON
        plaintext = json.dumps(user_data).encode('utf-8')
        
        # Prepare key and IV
        key = machine_id[:16].encode('utf-8')
        iv = key  # IV is same as key
        
        # Encrypt
        cipher = AES.new(key, AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(pad(plaintext, AES.block_size))
        
        # Encode to base64
        return base64.b64encode(ciphertext).decode('utf-8')
    
    def write_auth_files(self, token_info):
        """Write qodercli auth files"""
        # Use machineId from token or generate new one
        machine_id = token_info.get("machineId")
        if not machine_id:
            machine_id = str(uuid.uuid4())
        
        # Create user data
        user_data = self.create_user_data(token_info)
        
        # Encrypt user data
        encrypted_user = self.encrypt_user_data(user_data, machine_id)
        
        # Write machine_id file
        machine_id_file = self.auth_dir / "machine_id"
        machine_id_file.write_text(machine_id)
        machine_id_file.chmod(0o600)
        
        # Write encrypted user file
        user_file = self.auth_dir / "user"
        user_file.write_text(encrypted_user)
        user_file.chmod(0o600)
        
        print(f"✅ Injected auth for: {token_info['name']}")
        print(f"   Machine ID: {machine_id}")
        print(f"   User ID: {token_info['userId']}")
        print(f"   Auth files written to: {self.auth_dir}")
    
    def inject_token(self, token_index=0):
        """Inject a specific token by index"""
        tokens = self.get_9router_tokens()
        
        if not tokens:
            print("❌ No Qoder tokens found in 9Router database")
            return False
        
        if token_index >= len(tokens):
            print(f"❌ Token index {token_index} out of range (0-{len(tokens)-1})")
            return False
        
        token_info = tokens[token_index]
        self.write_auth_files(token_info)
        return True
    
    def list_available_tokens(self):
        """List all available tokens"""
        tokens = self.get_9router_tokens()
        
        print("=" * 60)
        print(f"Available Qoder Tokens: {len(tokens)}")
        print("=" * 60)
        
        for i, token in enumerate(tokens):
            print(f"{i:3d}. {token['name']}")
            print(f"     User ID: {token['userId']}")
            print(f"     Machine ID: {token['machineId']}")
        
        print("=" * 60)
        return tokens

def main():
    import sys
    
    injector = QoderAuthInjector()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "list":
            injector.list_available_tokens()
        elif sys.argv[1] == "inject":
            if len(sys.argv) > 2:
                token_index = int(sys.argv[2])
            else:
                token_index = 0
            injector.inject_token(token_index)
        else:
            print("Usage:")
            print("  python3 qodercli_auth_injector.py list")
            print("  python3 qodercli_auth_injector.py inject [index]")
    else:
        # Default: list tokens
        injector.list_available_tokens()

if __name__ == "__main__":
    main()
