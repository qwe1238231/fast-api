"""部署管線與告警接線的結構檢查。

這一組的失敗模式跟別處都不一樣:**壞掉的時候不會有任何東西變紅**。
  - 有人把 `needs: test` 拿掉 → 部署照跑,測試也照跑,只是不再互相等待。
  - 有人把 `print("ALERT ...")` 改成 `print("ALERT: ...")` → CloudWatch 的過濾器
    要求後面那個空格,於是那條告警從此永遠不會響,而它看起來跟「一直很平安」一樣。
  - 有人把 `--task-definition` 換回 `--force-new-deployment` → 部署仍然成功,只是
    再也說不出線上跑的是哪一個 commit。

所以這裡用純文字/結構斷言。它們不驗證 AWS 那端真的照做了(那要真的部署一次),
但它們鎖住的是**唯一會被人改壞、而且改壞了沒人會發現**的那一層。
"""
import ast
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
DEPLOY_YML = ROOT / ".github" / "workflows" / "deploy.yml"
TEST_YML = ROOT / ".github" / "workflows" / "test.yml"
MONITORING_TF = (ROOT / "infra" / "monitoring.tf").read_text()
SERVICES_TF = (ROOT / "infra" / "services.tf").read_text()


@pytest.fixture(scope="module")
def deploy() -> dict:
    return yaml.safe_load(DEPLOY_YML.read_text())


def _steps(deploy: dict) -> list[dict]:
    return deploy["jobs"]["deploy"]["steps"]


def _step_index(deploy: dict, needle: str) -> int:
    for i, step in enumerate(_steps(deploy)):
        if needle in (step.get("name") or ""):
            return i
    raise AssertionError(f"找不到名字含 {needle!r} 的步驟")


# ─ 閘門

def test_deploy_waits_for_the_tests(deploy) -> None:
    """部署必須等測試。

    舊寫法是 test.yml 與 deploy.yml 各自被同一個 push 觸發、平行跑 —— 所以「測試
    紅了」跟「已經部署上去了」可以同時成立,而且部署通常先完成(它不用裝 Rust
    toolchain)。紅色的勾勾出現時,壞掉的版本已經在線上服務使用者了。
    """
    needs = deploy["jobs"]["deploy"].get("needs")
    # `needs:` 兩種寫法都合法(字串或清單)—— 只認其中一種的斷言會在別人用另一種
    # 寫法時誤報,而誤報的測試最後一定會被註解掉。
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert "test" in needs
    assert deploy["jobs"]["test"]["uses"].endswith("test.yml")


def test_the_called_workflow_actually_runs_the_tests() -> None:
    """`needs: test` 只保證有一個叫 test 的工作先跑完 —— 不保證它跑了 pytest。"""
    called = yaml.safe_load(TEST_YML.read_text())
    # PyYAML 把 `on:` 解析成 True(YAML 1.1 的布林),所以兩個鍵都試。
    triggers = called.get("on") or called.get(True)
    assert "workflow_call" in triggers, "test.yml 沒有開放被呼叫"
    runs = " ".join(s.get("run", "") for s in called["jobs"]["test"]["steps"])
    assert "pytest" in runs


def test_only_one_deploy_can_run_at_a_time(deploy) -> None:
    """兩次快速推送會讓兩組 migration 與兩組 update-service 交錯,最後線上跑的是
    哪個 SHA 變成賽跑結果。而且不能 cancel-in-progress:取消一個「migration 已經
    下去、服務還沒滾」的部署,比讓它跑完更糟。"""
    concurrency = deploy["concurrency"]
    assert concurrency["group"]
    assert concurrency["cancel-in-progress"] is False


# ─ 部署本身

def test_migrations_run_before_the_code_rolls(deploy) -> None:
    """順序:先遷移、再換程式碼。

    反過來的話,新程式碼會有一段時間跑在舊 schema 上 —— 而那段時間裡壞掉的正好是
    剛部署的東西。代價是 migration 必須向後相容(舊程式碼會在新 schema 上再跑幾十
    秒),破壞性變更要拆成兩次部署。
    """
    assert _step_index(deploy, "migrations") < _step_index(deploy, "Roll ECS")


def test_a_failed_migration_stops_the_deploy(deploy) -> None:
    """遷移失敗必須讓工作流停在那裡。`aws ecs wait tasks-stopped` 只等它結束 ——
    **它不看退出碼**,所以少了那個明確的檢查,一個炸掉的 migration 會安靜地被
    當成成功,然後新程式碼照樣滾上去。"""
    script = _steps(deploy)[_step_index(deploy, "migrations")]["run"]
    assert "wait tasks-stopped" in script
    assert "exitCode" in script and "exit 1" in script


def test_services_are_pinned_to_a_task_definition(deploy) -> None:
    """服務要指向釘住 SHA 的修訂版,不是 `--force-new-deployment` + `:latest`。

    靠 :latest 的話:回滾要重推 image;而且一個在任何時間點被替換掉的任務會拉到
    「當時的」:latest —— 可能已經不是當初部署的那一版,沒有任何紀錄顯示它換過。
    """
    script = _steps(deploy)[_step_index(deploy, "Roll ECS")]["run"]
    assert "--task-definition" in script
    assert "--force-new-deployment" not in script


def test_the_deploy_waits_for_the_services_to_stabilise(deploy) -> None:
    """少了它,工作流在 update-service 回傳的那一刻就變綠 —— 而那只代表「ECS 收到
    了指令」。新任務一路 crash-loop,CI 仍然是綠的。"""
    script = _steps(deploy)[_step_index(deploy, "stabilise")]["run"]
    assert "wait services-stable" in script
    assert _step_index(deploy, "stabilise") > _step_index(deploy, "Roll ECS")


def test_a_failed_rollout_rolls_itself_back() -> None:
    """`wait services-stable` 逾時要能被安心解讀成「已經回滾了」而不是「線上是壞的」。
    那要靠服務上的 deployment_circuit_breaker,不是 CI 腳本 —— 放在 Terraform 才會
    對 console 手動發起的部署也生效。"""
    assert SERVICES_TF.count("deployment_circuit_breaker") == 3, "三個服務都要有"
    # 用正規式而不是精確字串:terraform fmt 會依區塊裡最長的屬性名調整 `=` 的對齊,
    # 所以哪天有人加一個長屬性,精確比對就會為了排版變紅。
    assert len(re.findall(r"rollback\s*=\s*true", SERVICES_TF)) == 3


def test_terraform_does_not_undo_the_deploy() -> None:
    """CI 會把服務指向新修訂版;不忽略這個欄位的話,下一次 terraform apply 會把它
    打回 TF 管的那一版(image 是 :latest)—— 也就是默默換掉剛部署的東西,而 plan
    上只顯示一行 task_definition 變更。"""
    assert len(re.findall(r"ignore_changes\s*=\s*\[task_definition\]", SERVICES_TF)) == 3


# ─ expand/contract:migration 必須能跟舊程式碼並存

#: 這些 migration 在規則存在之前就寫好了。它們當時是「停機部署」的假設下寫的,而
#: 那個假設在有 rolling deployment 之後不成立了。列成明確清單而不是「只檢查新的」,
#: 是因為後者需要一個基準修訂版,而那個基準會慢慢變成沒有人記得為什麼的魔法字串。
_PRE_RULE_MIGRATIONS = {
    "35846ecd3442",  # add check constraint on orders.status
    "7ae1b044057f",  # add positive-value checks
    "a248c0739bb0",  # add absolute_expires_at NOT NULL
    "a6528a2656ba",  # add check constraints on orders status/timestamps
    "b9a4fce2cc69",  # use SAEnum for status columns (alter → NOT NULL)
}

#: 滾動部署期間會讓**舊程式碼**壞掉的操作。判準是「舊 task 還在跑,它做得到的事
#: 會不會因為這次 schema 變更而失敗」。
#: drop_index / create_index / create_table / add_column(nullable=True) 不在裡面 ——
#: 那些舊程式碼完全感覺不到。
_BREAKING_OPS = {"drop_column", "drop_table", "rename_table"}


def _breaking_operations(upgrade: ast.FunctionDef) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(upgrade):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        name, src = node.func.attr, ast.unparse(node)
        if name in _BREAKING_OPS:
            found.add(f"{name}() — 舊程式碼還在讀/寫它")
        elif name == "add_column" and "nullable=False" in src and "server_default" not in src:
            found.add("add_column(nullable=False) 沒有 server_default — 舊程式碼的 INSERT 不會帶這一欄")
        elif name == "alter_column":
            if "nullable=False" in src:
                found.add("alter_column(nullable=False) — 舊程式碼可能還在寫 NULL")
            if "new_column_name" in src:
                found.add("改欄位名 — 對舊程式碼等於同時砍掉又新增")
        elif name == "create_check_constraint":
            found.add("create_check_constraint — 舊程式碼可能寫出違反它的列")
    return found


def _migrations() -> list[tuple[str, ast.Module]]:
    out = []
    for path in sorted((ROOT / "alembic" / "versions").glob("*.py")):
        out.append((path.name, ast.parse(path.read_text(), filename=str(path))))
    return out


def test_new_migrations_survive_a_rolling_deploy() -> None:
    """migration 先於程式碼跑,所以**舊程式碼會在新 schema 上再跑幾十秒**。

    這不是理論:滾動部署期間新舊 task 同時打同一個資料庫。在同一次部署裡砍掉一個
    欄位,那幾十秒內舊 task 的每一個 SELECT 都會炸 —— 而部署本身會顯示成功。

    破壞性變更要拆成兩次部署(expand:先加、雙寫;contract:下一次才刪)。真的需要
    contract 那一半時,在 migration 裡寫一行 `BACKWARD_INCOMPATIBLE = "理由"` ——
    用字串而不是布林旗標,是要逼作者寫下那句話:expand 的那一半是哪個修訂版、
    為什麼現在刪掉是安全的。

    這條測試存在的理由是:這個規則以前只寫在 deploy.yml 的註解裡,而註解不會變紅。
    """
    offenders = []
    for filename, tree in _migrations():
        revision = filename.split("_")[0]
        if revision in _PRE_RULE_MIGRATIONS:
            continue
        upgrade = next(
            (n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None
        )
        if upgrade is None:
            continue
        breaking = _breaking_operations(upgrade)
        if not breaking:
            continue
        acknowledged = any(
            isinstance(n, ast.Assign)
            and any(getattr(t, "id", None) == "BACKWARD_INCOMPATIBLE" for t in n.targets)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
            and n.value.value.strip()
            for n in tree.body
        )
        if not acknowledged:
            offenders.append(f"{filename}\n      " + "\n      ".join(sorted(breaking)))

    assert not offenders, (
        "這些 migration 在滾動部署期間會讓舊程式碼壞掉。拆成 expand/contract 兩次部署,"
        "或在檔案裡加一行 BACKWARD_INCOMPATIBLE = \"為什麼這次是安全的\":\n    "
        + "\n    ".join(offenders)
    )


def test_the_grandfather_list_does_not_rot() -> None:
    """清單裡的每一支都要真的還在,而且真的還有破壞性操作。

    少了這條,清單會慢慢變成一堆沒有人敢刪的字串 —— 有人把某支 migration 改乾淨了,
    它卻還被豁免著,於是下一次真的改壞就悄悄過關。
    """
    by_revision = {name.split("_")[0]: tree for name, tree in _migrations()}
    for revision in _PRE_RULE_MIGRATIONS:
        assert revision in by_revision, f"{revision} 已經不存在 —— 從清單移除"
        upgrade = next(
            (n for n in by_revision[revision].body
             if isinstance(n, ast.FunctionDef) and n.name == "upgrade"), None
        )
        assert upgrade is not None and _breaking_operations(upgrade), (
            f"{revision} 已經沒有破壞性操作 —— 從豁免清單移除,否則它會遮住未來的改動"
        )


# ─ 告警接線:程式碼印的字串必須真的被過濾器接到

def _alert_prefixes() -> set[str]:
    """從 monitoring.tf 抓出 `?"X" ?"Y"` 這種 OR 過濾器裡的字面值。"""
    match = re.search(r'pattern\s*=\s*"((?:\?\\"[^"]+?\\"\s*)+)"', MONITORING_TF)
    assert match, "找不到 ALERT/REFUND 的 OR 過濾器"
    return set(re.findall(r'\?\\"(.+?)\\"', match.group(1)))


def _printed_literals() -> list[tuple[str, int, str]]:
    """所有 print() 第一個參數的字面值開頭(含 f-string 的第一段)。"""
    out: list[tuple[str, int, str]] = []
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "print"
                    and node.args):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                head = first.value
            elif isinstance(first, ast.JoinedStr) and first.values and isinstance(
                first.values[0], ast.Constant
            ):
                head = first.values[0].value
            else:
                continue
            out.append((str(path.relative_to(ROOT)), node.lineno, head))
    return out


def test_the_filter_catches_every_alert_line_the_code_prints() -> None:
    """每一行 ALERT/REFUND 的輸出都必須被過濾器接到。

    **這條擋的是一個特別陰險的改動**:過濾器要的是 `"ALERT "`(帶尾隨空格)。
    有人把 `print("ALERT ...")` 改成 `print("ALERT: ...")`,程式碼看起來更整齊、
    log 讀起來一模一樣,而那條告警從此永遠不會響 —— 而「永遠不響」跟「一直很平安」
    在儀表板上長得完全一樣。
    """
    prefixes = _alert_prefixes()
    assert prefixes == {"ALERT ", "REFUND "}, f"過濾器的字面值變了:{prefixes}"

    unmatched = [
        f"{path}:{line} → {head[:60]!r}"
        for path, line, head in _printed_literals()
        if re.match(r"(ALERT|REFUND)", head)
        and not any(head.startswith(p) for p in prefixes)
    ]
    assert not unmatched, (
        "這些行印出了 ALERT/REFUND 但過濾器接不到(通常是尾隨空格沒了):\n  "
        + "\n  ".join(unmatched)
    )


def test_the_other_log_filters_still_match_real_lines() -> None:
    """另外兩條過濾器抓的是完整片語,一次無害的措辭調整就會讓它們失效。"""
    source = "\n".join(p.read_text() for p in APP.rglob("*.py"))
    for phrase in ("CIRCUIT-BREAKER admission paused", "INVENTORY DRIFT event"):
        assert f'pattern = "\\"{phrase}\\""' in MONITORING_TF, f"{phrase} 不在 tf 裡"
        assert phrase in source, f"{phrase} 已經不在程式碼裡 —— 那條告警永遠不會響"
