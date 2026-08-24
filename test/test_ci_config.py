"""CI 給的環境變數必須真的能讓 app 啟動。

**這條測試是被一次真實故障逼出來的。** 71bdd9b 為 `STRIPE_WEBHOOK_SECRET` 加了
fail-closed 驗證(空密鑰是「已知密鑰」,任何人都能偽造 Stripe webhook),同時改了
`infra/` 注入它 —— 但沒有改 CI。結果:

  - `alembic upgrade head` / `alembic check` 這幾步從那天起每次都在 Settings()
    驗證失敗
  - 而 `pytest` 照常綠,因為 conftest 自己 `setdefault("DEBUG", "True")`,
    DEBUG=True 讓那條守衛不成立

一半綠一半紅、而紅的那半沒人看,故障就這樣活了十八天。

守法是把 workflow 當資料讀:拿它宣告的 env,在**不讀 .env** 的情況下建一次
Settings。本機的 `.env` 什麼都有,所以只有 `_env_file=None` 才問得出「CI 那台機器
上到底夠不夠」。下一個人再加 fail-closed 驗證而忘記補 CI,這裡會當場紅。
"""
import pathlib

import pytest
import yaml

from app.core.config import Settings

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/test.yml"


def _job_env(job: str) -> dict[str, str]:
    """把某個 job 提供給步驟的 env 攤平(job 層級 + 各步驟自己的)。"""
    spec = yaml.safe_load(WORKFLOW.read_text())["jobs"][job]
    env = {k: str(v) for k, v in (spec.get("env") or {}).items()}
    for step in spec.get("steps", []):
        env.update({k: str(v) for k, v in (step.get("env") or {}).items()})
    return env


def test_the_ci_env_can_actually_build_settings(monkeypatch) -> None:
    env = _job_env("test")
    assert env, "test job 沒有宣告任何 env —— 這條測試就失去意義了"

    # 清掉本機環境:CI 那台機器只有 workflow 宣告的那些。
    for name in list(Settings.model_fields):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    # _env_file=None 是關鍵 —— 本機有 .env,不關掉的話這條測試永遠是綠的。
    Settings(_env_file=None)


def test_ci_runs_the_migration_steps_in_production_shape() -> None:
    """CI 不該靠 DEBUG=True 繞過 fail-closed 的守衛。

    那些守衛(webhook 密鑰、mock 付款、壓測旁路)存在的意義就是「production 形狀下
    設定不完整就拒絕啟動」。CI 如果整串都跑在 DEBUG=True 底下,等於把它們全部關掉,
    然後宣稱驗證過 —— 真正會炸的組態要到部署當下才發現。

    conftest 為了模擬付款仍會把 pytest 那一段設成 DEBUG=True,那是測試自己的事;
    workflow **不可以**在 job/步驟層級設死它,否則 alembic 那幾步也跟著失去意義。
    """
    spec = yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]
    assert "DEBUG" not in (spec.get("env") or {})
    for step in spec.get("steps", []):
        assert "DEBUG" not in (step.get("env") or {}), (
            f"步驟 {step.get('name')!r} 設了 DEBUG —— 那會讓 fail-closed 的守衛失效,"
            "而且 conftest 的 setdefault 也會因此不生效"
        )


def test_pushing_to_dev_actually_runs_the_tests() -> None:
    """分支流程是 feat → dev → main,先上 dev 確認沒問題才進 main。

    但那句話只有在「推 dev 真的會跑 CI」時才成立 —— 而它曾經不成立:push 的分支
    清單只有 `feat/*`,直接推 dev 什麼都不會觸發,於是「dev 綠了」是一個沒有對應
    run 的說法。規則用「推」來描述,觸發條件就必須跟著「推」走。

    `main` 反過來,刻意不在清單裡:deploy.yml 用 `uses:` 呼叫這整支,所以 main 上的
    測試是部署管線的第一關而不是平行的工作流。
    """
    on = yaml.safe_load(WORKFLOW.read_text())[True]     # YAML 把裸 `on:` 解析成 True
    branches = on["push"]["branches"]
    assert "dev" in branches, "推 dev 不會觸發 CI —— 分支流程的中間那一關是空的"
    assert "main" not in branches, "main 由 deploy.yml 以 workflow_call 帶起,不要重複觸發"


@pytest.mark.parametrize("required", ["DATABASE_URL", "STRIPE_WEBHOOK_SECRET"])
def test_the_env_is_declared_once_at_job_level(required: str) -> None:
    """env 必須在 job 層級宣告一次,不要每個步驟各複製一份。

    那個重複正是上面那次故障的根因:四個步驟各帶一份,新增一個必要設定時漏掉任何
    一個都不會有人發現,而且漏掉的那步失敗訊息跟「設定」看起來毫無關係。
    """
    spec = yaml.safe_load(WORKFLOW.read_text())["jobs"]["test"]
    assert required in (spec.get("env") or {})
