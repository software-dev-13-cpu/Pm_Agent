import getpass
import hashlib
import secrets

password = getpass.getpass("New password: ")
salt = secrets.token_hex(16)
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
print("\nAdd this line to pm_agent/.env:\n")
print(f"APP_PASSWORD_HASH={salt}${digest}")