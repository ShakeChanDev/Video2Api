"""SSE/流式接口鉴权工具。

说明：
- 流式接口不走 OAuth2PasswordBearer（token 从 query 传递），因此单独封装校验逻辑。
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, status
from jose import JWTError, jwt

from app.core.config import settings
from app.db.sqlite import sqlite_db


def require_user_from_query_token(token: Optional[str]) -> dict:
    """校验 query token，并返回 user dict；失败抛出 401。"""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少访问令牌")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        username = payload.get("sub")
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的访问令牌"
        ) from exc

    if not username:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的访问令牌")

    user = sqlite_db.get_user_by_username(str(username))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的访问令牌")
    return user
