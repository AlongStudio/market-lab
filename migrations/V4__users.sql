-- market_lab 用户表:用于登录鉴权(单用户场景,也支持多用户)。
-- password_hash 存 PBKDF2 派生格式 "pbkdf2_sha256$iterations$salt_b64$hash_b64"(见 app/auth.py)。
-- 初始用户由应用启动时按环境变量 AUTH_INIT_USER/AUTH_INIT_PASSWORD 自动播种(见 app/auth.py seed_initial_user)。

CREATE TABLE IF NOT EXISTS users (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
