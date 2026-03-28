# ==============================================================
# shared/auth.py
# JWT verification via AWS Cognito — used by ALL services
# ==============================================================
import boto3
import json
import os
import requests
from jose import jwk, jwt
from jose.utils import base64url_decode
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from functools import lru_cache
import logging

logger   = logging.getLogger(__name__)
security = HTTPBearer()

@lru_cache(maxsize=1)
def get_cognito_config():
    """Fetch Cognito config from Secrets Manager (cached)."""
    import boto3, json
    client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION","eu-north-1"))
    secret = json.loads(client.get_secret_value(SecretId="shivam-hospital/production/cognito")["SecretString"])
    return secret

@lru_cache(maxsize=1)
def get_jwks():
    """Fetch Cognito public keys (cached)."""
    config   = get_cognito_config()
    pool_id  = config["user_pool_id"]
    region   = pool_id.split("_")[0]
    url      = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
    response = requests.get(url)
    return response.json()["keys"]

def verify_token(token: str) -> dict:
    """Verify Cognito JWT token and return claims."""
    try:
        keys    = get_jwks()
        headers = jwt.get_unverified_headers(token)
        kid     = headers["kid"]

        # Find matching key
        key = next((k for k in keys if k["kid"] == kid), None)
        if not key:
            raise HTTPException(status_code=401, detail="Invalid token key")

        # Verify signature
        config   = get_cognito_config()
        pool_id  = config["user_pool_id"]
        region   = pool_id.split("_")[0]
        issuer   = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"

        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"verify_at_hash": False}
        )
        return claims

    except Exception as e:
        logger.error(f"Token verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")

def get_current_user(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict:
    """FastAPI dependency — returns current user from JWT."""
    return verify_token(credentials.credentials)

def require_role(allowed_roles: list):
    """FastAPI dependency factory — checks user role."""
    def role_checker(user: dict = Security(get_current_user)):
        groups = user.get("cognito:groups", [])
        if not any(role in groups for role in allowed_roles):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. Required roles: {allowed_roles}"
            )
        return user
    return role_checker

# Role dependencies
patient_only = require_role(["patients"])
doctor_only  = require_role(["doctors"])
admin_only   = require_role(["admins"])
staff_only   = require_role(["doctors", "admins"])
any_user     = require_role(["patients", "doctors", "admins"])
