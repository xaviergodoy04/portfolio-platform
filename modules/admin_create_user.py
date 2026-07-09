"""
CLI de administración de usuarios — único mecanismo para crear cuentas o
resetear contraseñas (sin registro público, sin endpoint HTTP). Uso:

    ./venv/bin/python -m modules.admin_create_user
"""
import getpass
import secrets
import sys

from modules import db


def _prompt_password(label: str = "Contraseña (Enter para generar una): ") -> tuple[str, bool]:
    """Retorna (password, fue_generada)."""
    password = getpass.getpass(label)
    if password:
        return password, False
    return secrets.token_urlsafe(9), True


def main():
    print("── Administración de usuarios ──")
    username = input("Usuario (sin espacios, ej: juan): ").strip().lower()
    if not username:
        print("❌ El usuario no puede estar vacío.")
        sys.exit(1)

    with db.db_conn() as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()

    if exists:
        answer = input(f"Ya existe '{username}'. ¿Resetear su contraseña? (s/N): ").strip().lower()
        if answer != "s":
            print("Cancelado.")
            sys.exit(0)
        password, generated = _prompt_password("Nueva contraseña (Enter para generar una): ")
        db.set_password(username, password)
        print(f"\n✅ Contraseña de '{username}' actualizada.")
        if generated:
            print(f"   Contraseña generada: {password}")
            print("   Guardala y pasásela por un canal seguro — no se vuelve a mostrar.")
        return

    display_name = input(f"Nombre para mostrar [{username}]: ").strip() or username
    password, generated = _prompt_password()
    user = db.create_user(username, password, display_name)

    print(f"\n✅ Usuario creado (id={user['id']}): {username}")
    if generated:
        print(f"   Contraseña generada: {password}")
        print("   Guardala y pasásela por un canal seguro — no se vuelve a mostrar.")


if __name__ == "__main__":
    main()
