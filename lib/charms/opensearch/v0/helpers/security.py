# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""Helpers for security related operations, such as password generation etc."""
import math
import os
import secrets
import string
import subprocess
import tempfile
from datetime import datetime
from typing import Optional, Tuple

import bcrypt
from cryptography import x509


def generate_password() -> str:
    """Generate a random password string.

    Returns:
       A random password string.
    """
    choices = string.ascii_letters + string.digits
    return "".join([secrets.choice(choices) for _ in range(32)])


def generate_hashed_password() -> Tuple[str, str]:
    """Generates a password and its bcrypt hash.

    Returns:
        A hash and the original password
    """
    pwd = generate_password()

    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd.encode("utf-8"), salt)

    return hashed.decode("utf-8"), pwd


def cert_expiration_remaining_hours(cert: string) -> int:
    """Returns the remaining hours for the cert to expire."""
    certificate_object = x509.load_pem_x509_certificate(data=cert.encode())
    time_difference = certificate_object.not_valid_after - datetime.utcnow()

    return math.floor(time_difference.total_seconds() / 3600)


def to_pkcs8(private_key: str, password: Optional[str] = None) -> str:
    """Convert a PEM key to PKCS8."""
    command = """openssl pkcs8 \
        -inform PEM \
        -outform PEM \
        -in {tmp_key_filename} \
        -topk8 \
        -v1 PBE-SHA1-3DES \
        -passout pass:"{password}" \
        -passin pass:"{password}" \
        -out {tmp_pkcs8_key_filename}"""
    if password is None:
        password = ""
        command = f"{command} -nocrypt"

    tmp_key = tempfile.NamedTemporaryFile(delete=False)
    tmp_pkcs8_key = tempfile.NamedTemporaryFile(delete=False)

    try:
        with open(tmp_key.name, "w") as f:
            f.write(private_key)

        subprocess.run(
            command.format(
                password=password,
                tmp_key_filename=tmp_key.name,
                tmp_pkcs8_key_filename=tmp_pkcs8_key.name
            ),
            shell=True,
            text=True,
            check=True,
            encoding="utf-8",
            env=os.environ,
        )

        with open(tmp_pkcs8_key.name, "r") as f:
            return f.read()
    finally:
        os.unlink(tmp_key.name)
        os.unlink(tmp_pkcs8_key.name)
