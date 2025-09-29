from fastapi import Header, HTTPException, status
from jose import jwt, JWTError
from app.settings import settings

ALGORITHM = "HS256"  # or RS256 if using public/private keys

def auth_token(authorization: str = Header(...)):
    """
    Expect: Authorization: Bearer <JWT>
    Verify the JWT with secret key.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header",
        )

    token = authorization.split(" ", 1)[1]

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        # Example: check a claim
        if "sub" not in payload:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing subject claim",
            )
        return payload  # so your endpoint can use user info
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token validation failed: {str(e)}",
        )
