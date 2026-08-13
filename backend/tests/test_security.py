"""S12新增: 安全相关单元测试。"""
import pytest
from app.core.security import hash_password, verify_password, create_access_token


class TestPasswordSecurity:
    """密码安全测试。"""

    def test_hash_and_verify_password(self):
        """密码哈希和验证。"""
        password = "TestPassword123!"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed) is True

    def test_wrong_password_fails(self):
        """错误密码验证失败。"""
        password = "CorrectPassword123!"
        wrong = "WrongPassword456!"
        hashed = hash_password(password)
        assert verify_password(wrong, hashed) is False

    def test_hash_is_unique(self):
        """相同密码两次哈希结果不同(bcrypt salt)。"""
        password = "SamePassword123!"
        hash1 = hash_password(password)
        hash2 = hash_password(password)
        assert hash1 != hash2
        assert verify_password(password, hash1) is True
        assert verify_password(password, hash2) is True


class TestJWTToken:
    """JWT Token 测试。"""

    def test_create_access_token_admin(self):
        """创建管理员访问令牌。"""
        token = create_access_token("admin", "admin")
        assert token is not None
        assert len(token) > 50
        assert "." in token

    def test_create_access_token_customer(self):
        """创建客户访问令牌。"""
        token = create_access_token("customer1", "customer")
        assert token is not None
        assert "." in token

    def test_tokens_are_unique(self):
        """不同 subject 生成不同 token。"""
        token1 = create_access_token("admin", "admin")
        token2 = create_access_token("user2", "customer")
        assert token1 != token2

    def test_token_with_extra_claims(self):
        """带额外声明的 token。"""
        token = create_access_token("admin", "admin", extra={"customer_id": 1})
        assert token is not None
        assert "." in token

    def test_different_subjects_different_tokens(self):
        """不同 subject 生成不同 token。"""
        token1 = create_access_token("admin", "admin")
        token2 = create_access_token("user1", "customer")
        assert token1 != token2
