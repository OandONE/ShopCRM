"""
ریست حساب مالک — برای وقتی رمز فراموش شده.
استفاده:
    python reset_owner.py <username> <new_password>

مثال:
    python reset_owner.py owner mynewpassword123
"""

import sys
import hashlib
import secrets
from sqlmodel import create_engine, Session, select
from models import User, UserSession


def hash_password(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python reset_owner.py <username> <new_password>")
        sys.exit(1)

    username = sys.argv[1].strip()
    password = sys.argv[2]

    if not username:
        print("❌ Username نمی‌تونه خالی باشه")
        sys.exit(1)
    if len(password) < 4:
        print("❌ رمز عبور باید حداقل ۴ کاراکتر باشه")
        sys.exit(1)

    engine = create_engine("sqlite:///database.db")

    with Session(engine) as s:
        owner = s.exec(select(User).where(User.role == "owner")).first()

        if owner:
            # update existing owner
            owner.username = username
            owner.password_hash = hash_password(password)
            owner.is_active = True
            owner.is_deleted = False
            s.add(owner)

            # invalidate all sessions
            for sess in s.exec(select(UserSession)).all():
                s.delete(sess)

            s.commit()
            print(f"✅ رمز مالک '{username}' به‌روز شد. تمام سشن‌ها باطل شدن")
        else:
            # create new owner
            new_owner = User(
                username=username,
                password_hash=hash_password(password),
                role="owner",
                full_name="مالک",
                is_active=True,
            )
            s.add(new_owner)
            s.commit()
            print(f"✅ مالک جدید با نام '{username}' ساخته شد")

    print("ℹ️  سرور رو ری‌استارت کن تا تغییرات اعمال بشه")


if __name__ == "__main__":
    main()
