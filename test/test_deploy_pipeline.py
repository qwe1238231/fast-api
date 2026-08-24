"""部署管線與告警接線的結構檢查。

這一組的失敗模式跟別處都不一樣:**壞掉的時候不會有任何東西變紅**。
  - 有人把 `needs: test` 拿掉 → 部署照跑,測試也照跑,只是不再互相等待。
  - 有人把 log 的 `event` 值改名(或改用 print) → CloudWatch 的欄位過濾器再也接
    不到,於是那條告警從此永遠不會響,而它看起來跟「一直很平安」一樣。
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
RDS_TF = (ROOT / "infra" / "rds.tf").read_text()
CICD_TF = (ROOT / "infra" / "cicd.tf").read_text()
VARIABLES_TF = (ROOT / "infra" / "variables.tf").read_text()
ECR_TF = (ROOT / "infra" / "ecr.tf").read_text()


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


def _code(hcl: str) -> str:
    """去掉 `#` 註解行,只留下真正生效的 HCL。

    這個檔案的斷言是純文字掃描,而這份程式碼庫的註解**很長而且會引用屬性名**
    (「原本設了 `force_delete = true` 所以才會出事」)。不濾掉的話,一段解釋為什麼
    不該這樣寫的註解,會讓「不准這樣寫」的測試變紅 —— 而修法會是刪掉那段解釋。

    同一類錯誤在這個專案發生過兩次(第一次是 `needs_human` 的生產者掃描掃到註解,
    後來改用 AST)。HCL 沒有現成的 AST,所以這裡用最小可行的做法:丟掉整行註解。
    """
    return "\n".join(
        line for line in hcl.splitlines() if not line.lstrip().startswith("#")
    )


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


def test_ci_checks_that_the_terraform_is_even_valid() -> None:
    """HCL 合不合法要在部署之前有人檢查。

    這個檔案裡其他的 infra 斷言掃的都是**文字**(「這個屬性有沒有被設成這個值」),
    它們對「這份 HCL 根本不合法」完全無感 —— 屬性名打錯、型別給錯、引用一個不存在的
    資源,全都能一路綠到 `terraform apply`,而 apply 只發生在部署當下、對著真的 AWS。

    跑在哪個 job 不重要(所以這裡掃全部的 job),重要的是它在這支工作流裡 —— 因為
    deploy.yml 是靠 `uses:` 呼叫整支來當閘門的。
    """
    called = yaml.safe_load(TEST_YML.read_text())
    runs = " ".join(
        step.get("run", "")
        for job in called["jobs"].values()
        for step in job.get("steps", [])
    )

    # 用正規表達式而不是子字串:實際的指令是 `terraform -chdir="$dir" validate`,
    # 中間夾著旗標。找 "terraform validate" 這個字面值會漏掉它。
    assert re.search(r"\bterraform\b[^\n]*\bvalidate\b", runs), (
        "CI 沒有跑 terraform validate —— `.tf` 的語法/型別錯誤要到部署當下的 apply 才會現形"
    )
    assert re.search(r"\bterraform\b[^\n]*\bfmt\b[^\n]*-check", runs), (
        "CI 沒有跑 terraform fmt -check(少了 -check 的話 fmt 只會改檔案然後回 0)"
    )
    assert "infra/bootstrap" in runs, (
        "validate 沒有涵蓋 infra/bootstrap。它是**另一個 root module**(自己的 state 與 "
        "lock),在 infra/ 底下跑的 validate 看不到它 —— 而它壞掉的後果是重建環境的前提"
        "沒了(見 infra/bootstrap/main.tf 的說明)。"
    )


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


def test_the_deploy_confirms_the_new_version_is_actually_live(deploy) -> None:
    """光等穩定不夠 —— 要確認線上跑的就是我們部署的那一版。

    `wait services-stable` 的條件是「只有一個 deployment 且 running == desired」,
    而斷路器**回滾成功之後服務也滿足這個條件**,只是滿足在舊版本上。

    2026-08-14 在 ap-northeast-2 實測(desiredCount=1):部署壞版本 15:43:00、斷路器
    15:52:29 跳、回滾 15:53:29 完成,而 waiter 的 600 秒上限在 15:53:00 —— 那一輪
    waiter 回了 255(CI 紅),但只差 **29 秒**它就會回 0。desiredCount 調大之後失敗
    平行累積、回滾更快,那 29 秒就變成負的。

    所以這條測試守的是「不要把安全性建立在復原比 waiter 慢上面」。
    """
    script = _steps(deploy)[_step_index(deploy, "stabilise")]["run"]
    assert "describe-services" in script and "taskDefinition" in script, (
        "wait 之後必須讀回線上的 task definition"
    )
    for service in ("api", "consumer", "worker"):
        assert f"steps.taskdefs.outputs.{service}" in script, (
            f"{service} 沒有跟部署的 ARN 比對"
        )
    assert "exit 1" in script, "比對不符必須讓這一步失敗"


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
    上只顯示一行 task_definition 變更。

    比對「清單裡有 task_definition」而不是「清單恰好等於 [task_definition]」——
    consumer 的清單還有 desired_count(交給 autoscaling 了),寫死整個清單會讓這條
    測試為了一個無關的正確改動而變紅。
    """
    ignored = re.findall(r"ignore_changes\s*=\s*\[([^\]]+)\]", SERVICES_TF)
    assert len(ignored) == 3, f"三個服務都要有 lifecycle,實際 {len(ignored)}"
    for fields in ignored:
        assert "task_definition" in fields


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
#: drop_index / create_table / add_column(nullable=True) 不在裡面 —— 那些舊程式碼
#: 完全感覺不到。create_index 只有 unique=True 才算(非 unique 的索引舊程式碼寫不壞)。
_BREAKING_OPS = {"drop_column", "drop_table", "rename_table"}

#: op.execute 原生 SQL 裡的破壞性語句。AST 比對 `op.<name>()` 看不到這些 ——
#: 而這個盲點已經漏掉過兩支真實的 migration(a3d5f81c 的 FK、e5b93c17 的整表重建,
#: 兩支都是自己補了 BACKWARD_INCOMPATIBLE 才留下紀錄)。
#:
#: (pattern 片語們, 說明, 建表豁免)。豁免 = 語句的目標表是**同一支 migration 建的**
#: 就不算破壞:舊程式碼根本不知道那張表,約束加在上面傷不到它(c41f8a2d5b70 的
#: EXCLUDE 就是這種)。DROP 與 RENAME 不豁免 —— e5b93c17 的 RENAME 目標雖是新表,
#: 但改名的結果是換掉舊程式碼正在用的名字,那正是破壞本身。
#: RENAME 要求同句有 ALTER TABLE:ALTER INDEX ... RENAME 不影響任何程式碼。
#: ALTER TABLE ... SET (storage 參數) 不在裡面,所以只認 SET NOT NULL 全詞。
_BREAKING_SQL: tuple[tuple[tuple[str, ...], str, bool], ...] = (
    (("DROP TABLE",), "DROP TABLE — 舊程式碼還在讀/寫它", False),
    (("DROP COLUMN",), "DROP COLUMN — 舊程式碼還在讀/寫它", False),
    (("ALTER TABLE", "RENAME"), "ALTER TABLE RENAME — 對舊程式碼等於同時砍掉又新增", False),
    (("SET NOT NULL",), "SET NOT NULL — 舊程式碼可能還在寫 NULL", True),
    (("ALTER COLUMN", " TYPE "), "ALTER COLUMN TYPE — 型別改變,舊程式碼的讀寫可能失敗", True),
    (("ADD CONSTRAINT", "CHECK"), "ADD CHECK — 舊程式碼可能寫出違反它的列", True),
    (("ADD CONSTRAINT", "FOREIGN KEY"), "ADD FOREIGN KEY — 舊程式碼可能寫出違反它的列", True),
    (("ADD CONSTRAINT", "EXCLUDE"), "ADD EXCLUDE — 舊程式碼可能寫出違反它的列", True),
    (("CREATE UNIQUE INDEX",), "CREATE UNIQUE INDEX — 舊程式碼可能寫出違反它的列", True),
    (("CREATE TRIGGER",), "CREATE TRIGGER — 對舊程式碼等於新的寫入約束", True),
    (("CREATE OR REPLACE TRIGGER",), "CREATE TRIGGER — 對舊程式碼等於新的寫入約束", True),
)

#: 從一句 SQL 撈出它動到的表,給建表豁免用。撈不到就當「不豁免」——
#: 寧可多標一條要人申報,不要少標。
_SQL_TARGET = re.compile(
    r"(?:ALTER\s+TABLE|CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+NOT\s+EXISTS\s+)?\S+\s+ON|TRIGGER\s+\S+[\s\S]*?\bON)\s+(\w+)",
    re.IGNORECASE,
)


def _downgrade_lines(tree: ast.Module) -> set[int]:
    """downgrade 函式體的行號範圍 —— 它本來就該裝著反向操作,不掃。"""
    lines: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def _literal_sql(node: ast.expr) -> str:
    """把 op.execute 的第一個參數還原成可比對的字串。

    f-string(JoinedStr)的變數部分換成佔位符 —— 我們比對的是 SQL 動詞,不是值。
    比對不了的形狀(變數名、函式呼叫)回空字串:那是這個掃描的已知盲點,連同
    「SQL 常數 import 自別的模組」一起,守不到的就靠 BACKWARD_INCOMPATIBLE 的
    自覺申報(e8f0a3d5 的 trigger 就是這種)。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
        )
    return ""


def _created_tables(tree: ast.Module) -> set[str]:
    """這支 migration 自己建的表(op.create_table + 原生 CREATE TABLE)。"""
    created: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "create_table" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                created.add(first.value.upper())
        elif node.func.attr == "execute" and node.args:
            for m in re.finditer(
                r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                _literal_sql(node.args[0]), re.IGNORECASE,
            ):
                created.add(m.group(1).upper())
    return created


def _breaking_operations(tree: ast.Module) -> set[str]:
    """掃整個模組(除了 downgrade)的 op.* 呼叫與 op.execute 字面 SQL。

    範圍是整個模組而不是只有 upgrade():兩支真實的 migration(c8f4a2e6、e5b93c17)
    都把操作包在模組層的 helper 裡,只看 upgrade() 就漏了 —— 而這個教訓對 op.*
    與原生 SQL **兩邊都成立**,不能只修一半。
    """
    skip = _downgrade_lines(tree)
    created = _created_tables(tree)
    found: set[str] = set()

    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.lineno not in skip
        ):
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
        elif name in ("create_index", "create_unique_constraint") and (
            name == "create_unique_constraint" or "unique=True" in src
        ):
            # 唯一性約束加在既有表上,舊程式碼可能寫出重複列。加在同一支 migration
            # 剛建的表上則豁免 —— 舊程式碼根本不知道那張表。
            table = next(
                (a.value for a in node.args[1:2]
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)),
                None,
            )
            if table is None or table.upper() not in created:
                found.add("唯一性約束/索引 — 舊程式碼可能寫出重複列")
        elif name == "execute" and node.args:
            sql = _literal_sql(node.args[0]).upper()
            if not sql:
                continue
            target = _SQL_TARGET.search(sql)
            target_created = target is not None and target.group(1).upper() in created
            for needles, label, exemptable in _BREAKING_SQL:
                if all(n in sql for n in needles) and not (exemptable and target_created):
                    found.add(label)
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
        breaking = _breaking_operations(tree)
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
        assert _breaking_operations(by_revision[revision]), (
            f"{revision} 已經沒有破壞性操作 —— 從豁免清單移除,否則它會遮住未來的改動"
        )


# ─ consumer 自動擴容:能擴的只有它,而上限由連線預算決定

AUTOSCALING_TF = (ROOT / "infra" / "autoscaling.tf").read_text()
TASKDEFS_TF = (ROOT / "infra" / "taskdefs.tf").read_text()


def test_the_worker_never_autoscales() -> None:
    """**worker 絕對不能有 autoscaling target。**

    它跑 ARQ 的 cron。兩個實例會讓每一條定時任務重複觸發 —— 過期掃描會把同一個座位
    釋放兩次,而那就是超賣。services.tf 已經用 max 100% / min 0% 把它鎖成跨部署的單例,
    這條測試守的是「沒有人為了對稱而順手給它也加一個 target」。

    (api 與 consumer 都可以擴,而且都擴。這條測試刻意只斷言 worker 不行 —— 上一版寫的
    是 `targets == ["consumer"]`,那把「當下有誰」跟「誰不准有」綁在一起,於是加一個
    合法的 target 也會讓它變紅,而變紅的訊息會說「只有 consumer 該擴」,完全誤導。)
    """
    targets = re.findall(r'resource\s+"aws_appautoscaling_target"\s+"(\w+)"', AUTOSCALING_TF)

    assert "worker" not in targets, f"worker 不能有 autoscaling target:{targets}"
    assert targets, "一個 autoscaling target 都沒有?"


def test_autoscaling_is_gated_so_a_plain_apply_still_works() -> None:
    """自動擴容必須是**預設關閉的開關**,而且**每一個**資源都吃到其中一個開關。

    2026-08-14 實測發現的:`terraform validate` 與 `plan` 都是綠的,`apply` 才炸 ——
    provider 的 default_tags 讓 RegisterScalableTarget 帶 Tags,而那需要
    `application-autoscaling:TagResource`;拿掉 tag 之後 provider 讀回狀態時還是會
    ListTagsForResource,同樣被拒。`AmazonECS_FullAccess` 不含這幾個 tag 動作。

    沒有這個開關的話,任何人的 apply 都會在這裡失敗,而錯誤訊息(AccessDenied on
    TagResource)跟「自動擴容」看起來毫無關係 —— 那是一顆地雷。開關讓「還沒打開」
    是一個明確的狀態。
    """
    variables_tf = (ROOT / "infra" / "variables.tf").read_text()
    for name in ("enable_consumer_autoscaling", "enable_api_autoscaling"):
        flag = re.search(rf'variable\s+"{name}"\s*\{{(.*?)\n\}}', variables_tf, re.S)
        assert flag, f"找不到 {name} 開關"
        assert re.search(r"default\s*=\s*false", flag.group(1)), f"{name} 必須預設關閉"
    # 缺的權限要寫在設定檔裡,不是只留在某次對話的記憶裡。
    for action in ("ListTagsForResource", "TagResource", "UntagResource"):
        assert action in variables_tf, f"缺的 IAM 權限 {action} 沒有寫下來"

    # 比對的是「有幾個資源」與「有幾個 count 吃到開關」。寫死成 5 個的話,每次加一組
    # 擴容都要改這個數字 —— 而改它的人正是那個可能忘記加 count 的人。
    gated = len(re.findall(r"count\s*=\s*var\.enable_\w*autoscaling", AUTOSCALING_TF))
    declared = len(re.findall(r'^resource\s+"', AUTOSCALING_TF, re.M))
    assert gated == declared, (
        f"{declared} 個資源但只有 {gated} 個吃開關 —— 漏掉的那個會讓 apply 照樣炸"
    )


def test_the_consumer_scales_on_the_queue_depth_not_cpu() -> None:
    """訊號必須是佇列深度。

    consumer 大部分時間阻塞在 XREADGROUP 上,落後的時候 CPU 一樣不高 —— 用
    CPUUtilization 當訊號會**永遠不觸發**,而那個設定看起來完全合理。
    """
    assert "order_stream_backlog" in AUTOSCALING_TF
    scaling_alarms = re.findall(
        r'resource\s+"aws_cloudwatch_metric_alarm"\s+"consumer_backlog_\w+"\s*\{(.*?)\n\}',
        AUTOSCALING_TF, re.S,
    )
    assert len(scaling_alarms) == 2, "要有擴出去與縮回去兩個告警"
    for body in scaling_alarms:
        assert "CPUUtilization" not in body
        assert 'metric_name = "order_stream_backlog"' in body


def test_scaling_in_is_slower_than_scaling_out() -> None:
    """縮回去必須比擴出去慢。

    擴錯的代價是幾分錢;縮錯的代價是在搶票尖峰中間把消費者拿掉,而被縮掉的那個實例
    正在處理的 entry 要等 reclaim 才會被接手 —— 那是分鐘級的延遲。
    """
    out = re.search(r'"consumer_out"\s*\{(.*?)\n\}\n\nresource', AUTOSCALING_TF, re.S)
    in_ = re.search(r'"consumer_in"\s*\{(.*?)\n\}\s*\Z', AUTOSCALING_TF, re.S)
    assert out and in_
    out_cooldown = int(re.search(r"cooldown\s*=\s*(\d+)", out.group(1)).group(1))
    in_cooldown = int(re.search(r"cooldown\s*=\s*(\d+)", in_.group(1)).group(1))
    assert in_cooldown > out_cooldown, f"縮 {in_cooldown}s 不該快於擴 {out_cooldown}s"

    low = re.search(r'"consumer_backlog_low"\s*\{(.*?)\n\}', AUTOSCALING_TF, re.S).group(1)
    high = re.search(r'"consumer_backlog_high"\s*\{(.*?)\n\}', AUTOSCALING_TF, re.S).group(1)
    low_periods = int(re.search(r"evaluation_periods\s*=\s*(\d+)", low).group(1))
    high_periods = int(re.search(r"evaluation_periods\s*=\s*(\d+)", high).group(1))
    assert low_periods > high_periods, "縮回去要看更久才確定真的沒事了"


def test_terraform_does_not_undo_the_autoscaling() -> None:
    """desired_count 交給 autoscaling 之後 Terraform 不能再管它 —— 否則下一次 apply
    會把擴出去的實例數打回 1,而 plan 上只有一行 desired_count。跟 task_definition
    是同一個坑,這裡是它的第二個實例。"""
    consumer_block = SERVICES_TF.split('resource "aws_ecs_service" "consumer" {')[1]
    consumer_block = consumer_block.split('resource "aws_ecs_service"')[0]
    assert re.search(r"ignore_changes\s*=\s*\[task_definition,\s*desired_count\]", consumer_block)


def _pool_per_task(service: str) -> int:
    """`{service}_pool_env` 宣告的 pool + overflow = 一個任務的連線上界。"""
    block = re.search(rf"{service}_pool_env = \[(.*?)\n  \]", TASKDEFS_TF, re.S)
    assert block, f"taskdefs.tf 裡找不到 {service}_pool_env"
    size = int(re.search(r'"DB_POOL_SIZE", value = "(\d+)"', block.group(1)).group(1))
    overflow = int(
        re.search(r'"DB_MAX_OVERFLOW", value = "(\d+)"', block.group(1)).group(1)
    )
    return size + overflow


def _target_block(service: str) -> str:
    block = re.search(
        rf'resource "aws_appautoscaling_target" "{service}" \{{(.*?)\n\}}',
        AUTOSCALING_TF,
        re.S,
    )
    assert block, f"autoscaling.tf 裡找不到 {service} 的 autoscaling target"
    return block.group(1)


def _max_capacity(service: str) -> int:
    return int(re.search(r"max_capacity\s*=\s*(\d+)", _target_block(service)).group(1))


def _min_capacity(service: str) -> int:
    return int(re.search(r"min_capacity\s*=\s*(\d+)", _target_block(service)).group(1))


def test_the_scaling_ceiling_respects_the_connection_budget() -> None:
    """擴容上限乘上每個實例的連線池,不能超過 RDS 的 max_connections。

    這條算術是**唯一**擋住「把 max_capacity 調大就好」的東西。db.t4g.micro 只有約
    112 條連線;把它用光的症狀是「全站 500」,而那看起來跟擴容完全無關 —— 沒有人會
    想到去查一個 autoscaling 設定。

    **每個數字都從 .tf 讀出來**,一個都不寫死。舊版把 api/worker/migration 那三格寫成
    常數(`2 * 15`、`15`、`15`),於是「改了 task def 的池子」跟「這條測試算的東西」
    可以無聲地分家 —— 而這條測試的全部價值就在於它們不會分家。
    """
    api_peak = _max_capacity("api") * 2 * _pool_per_task("api")  # 部署期間 max 200%
    worker_peak = _pool_per_task("worker")                       # 單例
    consumer_peak = _max_capacity("consumer") * _pool_per_task("consumer")
    # 部署時的 migration 是 one-off task,用的是 **worker 的** task def(deploy.yml)。
    migration_peak = _pool_per_task("worker")
    budget = 112           # db.t4g.micro: LEAST(DBInstanceClassMemory/9531392, 5000)

    total = api_peak + worker_peak + consumer_peak + migration_peak
    assert total <= budget - 10, (
        f"最壞情況要 {total} 條連線(api {api_peak} / worker {worker_peak} / "
        f"consumer {consumer_peak} / migration {migration_peak}),而 db.t4g.micro 只有 "
        f"~{budget} —— 調高 max_capacity 之前要先縮某個 pool、加 RDS Proxy,或換大一號"
    )


def test_api_scale_in_cannot_fire_during_a_sale_window() -> None:
    """縮容的告警必須被 `sale_imminent` 遮住。

    **這是整組預熱裡最容易無聲失效的地方。** 預熱期間 CPU 本來就是低的(人還沒到),
    所以一條單純看 CPU 的縮容告警會在開賣前把剛拉起來的容量收回去 —— 預熱因此完全
    失效,而外觀上「預熱有做、縮容也有做」,兩邊各自都合理。

    `FILL(...,0)` 同樣是必要的:sale_imminent 只在 worker 活著時有資料點,缺資料會讓
    整個 metric math 變成沒有資料,那時縮容不是「暫停」而是**永遠不會發生**。
    """
    alarm = re.search(
        r'resource\s+"aws_cloudwatch_metric_alarm"\s+"api_low_load"\s*\{(.*?)\n\}\n',
        AUTOSCALING_TF, re.S,
    )
    assert alarm, "找不到 api 的縮容告警"
    body = alarm.group(1)

    expression = re.search(r"expression\s*=\s*\"(.*?)\"", body)
    assert expression, "縮容告警必須用 metric math,不能直接看 CPU"
    assert "sale_imminent" in body, "縮容的判斷裡沒有開賣訊號 —— 預熱會被它收回去"
    assert "FILL(" in expression.group(1), (
        "sale_imminent 缺資料時整個運算式會變成沒有資料,縮容就永遠不會發生"
    )


def test_prewarm_sets_capacity_to_the_ceiling() -> None:
    """預熱必須用 ExactCapacity,而且值要等於 max_capacity。

    用 ChangeInCapacity(「再加兩個」)的話,開賣前的容量取決於當下有幾個任務 ——
    那是一個看運氣的數字,而預熱要的就是「不看運氣」。
    """
    policy = re.search(
        r'resource\s+"aws_appautoscaling_policy"\s+"api_prewarm"\s*\{(.*?)\n\}\n',
        AUTOSCALING_TF, re.S,
    )
    assert policy, "找不到預熱的 scaling policy"
    body = policy.group(1)

    assert 'adjustment_type = "ExactCapacity"' in body
    adjustment = int(re.search(r"scaling_adjustment\s*=\s*(\d+)", body).group(1))
    assert adjustment == _max_capacity("api"), (
        f"預熱設到 {adjustment} 但上限是 {_max_capacity('api')} —— 兩個數字必須一起改"
    )


def test_api_has_ha_even_with_autoscaling_switched_off() -> None:
    """HA 不能取決於 autoscaling 那個開關。

    開關預設是關的(IAM 需求,見另一條測試)。如果 `desired_count` 還是 1,那麼在
    「還沒補 IAM」的狀態下整個 api 是單點 —— 而那是最容易長期停留的狀態。
    """
    api_block = SERVICES_TF.split('resource "aws_ecs_service" "api" {')[1]
    api_block = api_block.split('resource "aws_ecs_service"')[0]

    desired = int(re.search(r"desired_count\s*=\s*(\d+)", api_block).group(1))
    assert desired >= 2, "單一任務 = 單一 AZ = 單點故障"
    assert desired == _min_capacity("api"), (
        "services.tf 的 desired_count 與 autoscaling 的 min_capacity 必須一致,"
        "否則打開開關的那一刻容量會跳動"
    )
    # 擴出去之後 apply 不能把它打回來 —— 跟 consumer 同一個坑的第三個實例。
    assert re.search(r"ignore_changes\s*=\s*\[task_definition,\s*desired_count\]", api_block)


def test_the_metric_names_the_alarms_read_are_the_ones_the_app_publishes() -> None:
    """告警讀的指標名必須是程式碼真的發出來的那個。

    跨語言的字串耦合,跟 log 的 `event` 欄位是同一類問題:改掉 Python 端的常數不會讓
    任何測試變紅,而 CloudWatch 那邊只會安靜地看不到資料(然後 treat_missing_data
    讓它永遠停在 notBreaching)。
    """
    import app.worker as worker

    published = {
        worker.METRIC_NAME_BACKLOG,
        worker.METRIC_NAME_DEAD_LETTER_NEW,
        worker.METRIC_NAME_SALE_IMMINENT,
    }
    referenced = set(re.findall(r'metric_name\s*=\s*"(\w+)"', AUTOSCALING_TF + MONITORING_TF))

    orphans = {
        name for name in referenced
        if name.islower() and name not in published        # AWS/ECS 的指標是駝峰
    }
    assert not orphans, f"這些指標有告警但程式碼發不出來:{sorted(orphans)}"


# ─ ALB:健康檢查通過的意思,以及「一條 SSE 是一個永不結束的請求」

ALB_TF = (ROOT / "infra" / "alb.tf").read_text()
MAIN_PY = APP / "main.py"
SSE_MODULE = APP / "api" / "v1" / "events.py"

# 一個死掉的 target 還能收到流量多久 = interval × unhealthy_threshold。搶票的尖峰
# 常常只有幾十秒,所以這個上界是照著業務的時間尺度定的,不是照著 AWS 的預設值。
_DEAD_TARGET_TRAFFIC_CEILING_SECONDS = 30

# 這個專案的依賴一律走 DI 標註進來(app/api/deps.py),所以標註就是契約。
_SHARED_DEPENDENCY_ANNOTATIONS = {"DbSession", "Redis", "AsyncSession"}


def _health_check() -> dict[str, str]:
    """把 alb.tf 的 health_check 區塊解析成 key -> value(值都當字串)。"""
    block = _code(ALB_TF).split("health_check {")[1].split("}")[0]
    return dict(re.findall(r'(\w+)\s*=\s*"?([\w/\-]+)"?', block))


def _route_handler(path: str) -> ast.FunctionDef:
    """找出 main.py 裡掛在 `path` 上的處理函式。

    找不到本身就是一個嚴重的失效:ALB 探測一條不存在的路徑會拿到 404,於是**每一個**
    target 都變 unhealthy —— 服務其實是好的,但沒有後端收得到流量。
    """
    tree = ast.parse(MAIN_PY.read_text(), filename=str(MAIN_PY))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "get"
                and deco.args
                and isinstance(deco.args[0], ast.Constant)
                and deco.args[0].value == path
            ):
                return node
    raise AssertionError(
        f"alb.tf 探測 {path},但 {MAIN_PY.name} 沒有這條 GET 路由。ALB 會拿到 404,"
        "於是所有 target 同時 unhealthy —— 程式是好的,流量卻沒有後端可以送。"
        "(路由若搬到 router 底下,連同這條測試的搜尋範圍一起改。)"
    )


def test_the_endpoint_the_alb_polls_touches_no_shared_dependency() -> None:
    """ALB 探測的那支端點**不能**碰 DB 或 Redis,而且 matcher 必須是單一的 200。

    這兩件事是同一個決定的兩半。

    深度:健康檢查同時決定「送不送流量」與(接上 ECS 之後)「殺不殺這個任務」。DB 是
    共用的,所以深度檢查會在資料庫一抖時讓**每一個** target 同時 unhealthy:ALB 沒有
    後端可送(完全中斷),而 ECS 把所有任務殺掉重啟,重啟再回頭捶正在恢復的資料庫。
    一次依賴抖動被放大成全面停機加驚群。這條擋的是好意的改動 —— 「健康檢查不查資料庫
    也太淺了吧」是一個非常自然的念頭,而它會在最壞的時刻生效。

    matcher:舊設定是 path="/" + matcher="200-399",而 "/" 回 307 轉去 /docs ——
    「健康」的實際判準變成「那個轉址還在」。範圍型的 matcher 會讓語意搭在一個沒人
    當成契約的行為上。單一 200 是明確的契約。

    深度檢查在 /health/deps(它**應該**碰依賴),消費者是值班的人與部署後的煙霧測試。
    """
    hc = _health_check()

    assert hc["matcher"] == "200", (
        f"matcher={hc['matcher']!r} 是範圍。範圍會讓「健康」的判準搭上轉址之類沒人當成"
        "契約的行為 —— 那正是 path=\"/\" + 200-399 的舊設定出過的問題。"
    )

    handler = _route_handler(hc["path"])

    # 舊設定真正的失效是這個:探測的那支回 307。matcher 收緊成 200 之後,把 path 改回
    # 一支會轉址的端點就等於讓所有 target 一起 unhealthy —— 而改 path 的那一行 diff
    # 看不出來這件事。
    assert "RedirectResponse" not in ast.unparse(handler), (
        f"{handler.name}() 會轉址,但 ALB 探測它而且只接受 200 —— 所有 target 會同時"
        "unhealthy。ALB 要探的是一支專屬的存活端點,不是任何剛好存在的路由。"
    )

    offenders = {
        arg.arg: ast.unparse(arg.annotation)
        for arg in handler.args.args + handler.args.kwonlyargs
        if arg.annotation is not None
        and ast.unparse(arg.annotation) in _SHARED_DEPENDENCY_ANNOTATIONS
    }
    assert not offenders, (
        f"{handler.name}() 收了共用依賴 {offenders} —— 但 ALB 正在探測它。資料庫抖一下,"
        "所有 target 會同時 unhealthy(完全中斷)而且被 ECS 全部重啟,重啟再去捶正在"
        "恢復的資料庫。任務重啟只治得好「這個 process 自己壞了」。要做深度檢查請放到 "
        "/health/deps,不要放進 ALB 探測的這一支。"
    )


def test_a_dead_target_stops_getting_traffic_quickly() -> None:
    """`interval × unhealthy_threshold` 是一個死掉的 target 還能繼續吃流量的時間。

    AWS 的預設(30 × 3 = 90 秒)對一般服務沒問題,對搶票不行 —— 一整段開賣可能還撐
    不到 90 秒,那等於整場都有一台在丟請求。而這個代價是不對稱的:探測變密只是多幾個
    HTTP 請求,而那支端點不碰任何依賴(見上一條),所以幾乎沒有成本。
    """
    hc = _health_check()
    interval, unhealthy = int(hc["interval"]), int(hc["unhealthy_threshold"])
    window = interval * unhealthy

    assert window <= _DEAD_TARGET_TRAFFIC_CEILING_SECONDS, (
        f"interval={interval} × unhealthy_threshold={unhealthy} = {window} 秒,超過 "
        f"{_DEAD_TARGET_TRAFFIC_CEILING_SECONDS} 秒的上界:一台死掉的 task 會吃掉這麼久的"
        "流量。探測密一點的成本只有多幾個 /health 請求,而那支不碰依賴。"
    )
    # ALB 自己就要求 timeout < interval。寫在這裡是為了讓它在 plan 之前就紅 ——
    # 這種錯目前的 CI 抓不到(沒有跑 terraform validate)。
    assert int(hc["timeout"]) < interval, "timeout 必須小於 interval"


def _sse_max_seconds() -> int:
    """從 events.py 取 `_SSE_MAX_SECONDS`,而且用 AST 不用正規表達式。

    理由跟 `needs_human` 那次一樣:文字掃描會掃到註解與字串,而這個常數那一行後面
    正好跟著一段解釋。AST 只看真的賦值。
    """
    tree = ast.parse(SSE_MODULE.read_text(), filename=str(SSE_MODULE))
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "_SSE_MAX_SECONDS" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, int)
        ):
            return node.value.value
    raise AssertionError(
        f"{SSE_MODULE.name} 找不到 _SSE_MAX_SECONDS 的整數賦值 —— 少了它,SSE 連線沒有"
        "壽命上限,而部署的排空時間也就失去了上界"
    )


def test_the_sse_lifetime_cap_is_actually_enforced() -> None:
    """常數存在不等於它還在管事。

    這條擋的是「有人把 `while` 條件改掉、常數卻留在原地」—— 那會讓下面那條不變式
    在對照一個已經沒有效力的數字,而兩條測試都還是綠的。
    """
    source = SSE_MODULE.read_text()
    assert source.count("_SSE_MAX_SECONDS") >= 2, (
        "_SSE_MAX_SECONDS 只出現在賦值處,沒有任何地方讀它 —— 連線壽命上限已經失效"
    )


def test_the_drain_window_stays_inside_the_sse_lifetime() -> None:
    """`deregistration_delay` 必須**小於** `_SSE_MAX_SECONDS`。

    對 ALB 來說一條 SSE 就是一個永遠不結束的請求,所以下線一個 target 時,排空會等
    這些串流。真正生效的是 `min(deregistration_delay, 串流剩餘壽命)`,而它只有兩種
    形態,兩種形態就是兩個不同的決定:

      delay >= 上限  串流永遠不會被硬切,每條都自己走完;部署最久等滿「上限」。
      delay <  上限  活超過 delay 的串流被強制關閉;部署最久等 delay。

    這裡刻意選後者:重連在**這個** app 是安全的,因為重連後第一個 frame 就是
    queue_status 的權威狀態,而入場是時間的純函數 —— 連線本身不保存任何東西。
    換一個把狀態放在連線裡的系統,同樣的數字就是錯的。所以要鎖的不是「60」,是
    「60 落在 300 的哪一邊」。

    為什麼非得用測試鎖:三種改壞的方式(把 SSE 上限拉到 900、降到 30、或把
    deregistration_delay 刪掉吃預設值)**兩邊各自看都是對的**,每次的 diff 都只有
    一行,審的人看不到另一個檔案。這個關係不在任何型別、任何 plan、任何 diff 裡,
    只寫在 alb.tf 的一段註解 —— 而註解不會失敗。
    """
    match = re.search(r"deregistration_delay\s*=\s*(\d+)", _code(ALB_TF))
    assert match, (
        "alb.tf 沒有設 deregistration_delay。預設值 300 秒剛好等於 _SSE_MAX_SECONDS,"
        "於是每次部署都為了排空長連線等滿五分鐘 —— 而那個五分鐘看起來會像 ECS 很慢,"
        "不像一個設定。要維持預設也請明確寫出來。"
    )

    delay, sse_max = int(match.group(1)), _sse_max_seconds()

    assert delay < sse_max, (
        f"deregistration_delay={delay} 沒有小於 _SSE_MAX_SECONDS={sse_max}:每次部署都會"
        f"為了等 SSE 自己結束而多花最多 {sse_max} 秒,而那個延遲的原因在 events.py,"
        "不在任何 Terraform 檔案裡。要改成「等串流走完」是一個合理的決定,但請連同 "
        "alb.tf 的註解一起改 —— 不要只把數字調到這條測試變綠。"
    )
    assert delay > 0, (
        "0 = 不排空,連一般的 POST /orders 都會在部署當下被硬切"
    )


# ─ 資料耐久性:這些設定被改回「dev 值」不會讓任何功能壞掉,只會讓復原能力消失

def test_backups_cannot_be_switched_off() -> None:
    """備份保留期必須 > 0,而且**不能只靠預設值**。

    `backup_retention_period = 0` 就是關閉 PITR,而 PITR 是「壞掉的 migration 已經跑完
    了」唯一的解法 —— schema 可以 alembic downgrade,資料回不來。這個值原本就是 0,
    註解寫著「dev: no backups (cheap, clean destroy)」,所以「為了方便關掉它」不是
    假想的風險,而是這個檔案的歷史。

    所以除了預設值,還要有 validation 把下限釘死:預設值只是預設,任何人加一行
    `db_backup_retention_days = 0` 到 tfvars 就繞過了。
    """
    var_block = re.search(
        r'variable\s+"db_backup_retention_days"\s*\{(.*?)\n\}', VARIABLES_TF, re.S
    )
    assert var_block, "找不到 db_backup_retention_days"
    default = int(re.search(r"default\s*=\s*(\d+)", var_block.group(1)).group(1))
    assert default >= 7, f"PITR 窗只有 {default} 天"
    assert "validation" in var_block.group(1), (
        "沒有 validation 的話,tfvars 裡一行 = 0 就把備份關掉了,而 plan 上看起來無害"
    )
    assert re.search(r">=\s*7", var_block.group(1)), "validation 沒有把下限釘在 7 天"

    assert "backup_retention_period = var.db_backup_retention_days" in RDS_TF, (
        "實例沒有用那個變數 —— validation 就白寫了"
    )


def test_destroy_leaves_a_recoverable_snapshot() -> None:
    """`terraform destroy` 必須留下最終快照,而且名字要唯一。

    團隊的工作方式是「每次 session 結束就 destroy」(為了省錢)。在此之前那個動作等於
    永久丟掉所有資料 —— 而它每天都會被執行一次。

    名字唯一是同樣重要的一半:同名快照已存在時,下一次 destroy 會**在拆到一半時**失敗。
    """
    # 用 regex 而不是比對精確空白:terraform fmt 會對齊等號,所以只要同一個 block 裡
    # 多一個較長的屬性名,寫死空白的斷言就會為了完全無關的理由變紅。
    assert re.search(r"skip_final_snapshot\s*=\s*false", RDS_TF), (
        "skip_final_snapshot 必須是 false,否則 destroy 不留任何東西"
    )
    assert re.search(
        r"final_snapshot_identifier\s*=\s*\".*random_id\.db_final_snapshot", RDS_TF
    ), "最終快照的名字必須帶隨機後綴,否則第二次 destroy 會撞名而失敗"


def test_deletion_protection_is_on_and_the_way_around_it_is_documented() -> None:
    """刪除保護預設開啟,**而且解開它的指令必須寫在 README 裡**。

    兩個斷言是一體的。這個保護每次 session 結束都會擋路,所以它能不能活下來完全取決於
    「繞過的摩擦有多小」—— 一個每天擋路又沒人知道怎麼正確繞過的保護,最後一定會被永久
    關掉,而那時它還留在程式碼裡,看起來像有保護。

    它擋的是一份寫著 `# forces replacement` 的 plan 被草率核准:RDS 重建是先刪再建,
    結果是最終快照有拍到、但新實例是空的。
    """
    var_block = re.search(
        r'variable\s+"db_deletion_protection"\s*\{(.*?)\n\}', VARIABLES_TF, re.S
    )
    assert var_block, "找不到 db_deletion_protection"
    assert re.search(r"default\s*=\s*true", var_block.group(1)), "刪除保護必須預設開啟"

    readme = (ROOT / "infra" / "README.md").read_text()
    assert "-var db_deletion_protection=false" in readme, (
        "README 沒有寫兩步 destroy 的指令 —— 下一個被擋到的人會直接把預設值改掉"
    )
    assert "-target=aws_db_instance.main" in readme, (
        "少了 -target,解開保護那一步會 apply 整份設定 —— 慢,而且會順手套用別的變更"
    )


def test_the_database_is_encrypted_and_highly_available() -> None:
    """靜態加密與 Multi-AZ 都是**只能在建立時決定**的性質。

    加密之後想關、或沒加密想開,都要走「快照 → 還原 → 換端點」,沒有原地切換。
    Multi-AZ 可以事後開,但它同時是「備份從待命實例產生、不影響服務」的前提 ——
    而部署前拍快照這件事就靠那個性質。

    Multi-AZ 走變數(還原時要能關掉,見 db_multi_az),所以這裡要鎖兩件事:接線對、
    **而且預設是 true**。只驗接線的話,有人把 default 改成 false 仍然全綠 —— 而那會
    讓 HA 從「預設有」變成「要記得開」。
    """
    rds = _code(RDS_TF)
    assert "storage_encrypted = true" in rds
    assert re.search(r"multi_az\s*=\s*var\.db_multi_az", rds), (
        "multi_az 必須接到 db_multi_az 變數,否則還原時關不掉(實測佔 RTO 的 74%)"
    )
    var_block = re.search(r'variable\s+"db_multi_az"\s*\{(.*?)\n\}', VARIABLES_TF, re.S)
    assert var_block, "找不到 db_multi_az"
    assert re.search(r"default\s*=\s*true", var_block.group(1)), (
        "db_multi_az 必須預設 true —— 那個變數只有還原時才該關掉"
    )


def test_task_defs_wait_for_the_secret_to_have_a_value() -> None:
    """三個 task def 都必須 `depends_on` 秘密的**版本**,而不是只引用秘密本身。

    2026-08-18 還原演練實測到的失敗:`valueFrom` 引用的是 `aws_secretsmanager_secret`
    —— 也就是「殼」。殼在 `CreateSecret` 之後就存在,但**值**要等
    `aws_db_instance.main.address`,而 Multi-AZ 的 RDS 要十幾分鐘。依賴圖因此允許
    「ECS 服務先建好、秘密的值後寫入」,於是:

        02:08:12  任務啟動失敗 ResourceNotFoundException: ... staging label: AWSCURRENT
        02:11:10  deployment circuit breaker 判定失敗,**放棄且不再重試**
        02:12:19  Terraform 寫入秘密版本 → apply 全綠

    輸了 69 秒。之後環境每一項設定都正確,而三個服務全死,唯一症狀是 ALB 的 503。

    這個測試存在的理由是它**只在賽跑輸掉時才會顯現**:RDS 快一點就會過,所以真實
    症狀是「有時候環境開起來是死的」—— 那種 bug 會被當成「重跑一次就好」而永遠留著。
    """
    for name in ("api", "consumer", "worker"):
        block = re.search(
            rf'resource\s+"aws_ecs_task_definition"\s+"{name}"\s*\{{(.*?)\n\}}',
            _code(TASKDEFS_TF),
            re.S,
        )
        assert block, f"找不到 {name} 的 task definition"
        assert "aws_secretsmanager_secret_version.app" in block.group(1), (
            f"{name} 的 task def 少了 depends_on 秘密版本 —— "
            "環境會以「apply 全綠但服務全死」的方式壞掉,而且不是每次都壞"
        )


def test_the_image_registry_does_not_share_the_environment_lifecycle() -> None:
    """主設定必須**引用** ECR,不能擁有它。

    2026-08-18 演練:`terraform destroy` 把 repository 連映像一起刪掉,於是重建的環境
    資料還原成功、`apply` 全綠,而 ALB 一直回 503 ——
    `CannotPullContainerError: ... :latest: not found`。

    分類錯誤:映像倉庫是「重建環境的**輸入**」,不是環境的一部分。跟 cicd.tf 對
    GitHub OIDC provider 的判斷同源 —— 引用而不是擁有。

    這個測試會紅的情境很具體:有人為了「少一個 bootstrap 步驟」把 repository 搬回主
    設定。那個改動在 apply 當下完全正常,代價要到**下一次重建環境**才出現。
    """
    ecr = _code(ECR_TF)
    assert 'data "aws_ecr_repository" "app"' in ecr, "主設定必須用 data source 引用 ECR"
    assert 'resource "aws_ecr_repository"' not in ecr, (
        "主設定不能擁有 ECR —— destroy 會把重建環境所需要的映像一起帶走"
    )

    bootstrap = ROOT / "infra" / "bootstrap" / "main.tf"
    assert bootstrap.exists(), "ECR 搬走了,但 infra/bootstrap/ 不見了"
    src = _code(bootstrap.read_text())
    assert 'resource "aws_ecr_repository" "app"' in src
    # 比對「有沒有設成 true」而不是「有沒有出現 force_delete 這個字」—— 註解裡解釋
    # 為什麼不設它是好事,不該讓測試變紅。
    assert not re.search(r"force_delete\s*=\s*true", src), (
        "bootstrap 的 repository 不能設 force_delete = true —— "
        "有映像時 destroy 失敗正是想要的守衛,那是它從主設定搬過來的全部理由"
    )

    readme = (ROOT / "infra" / "README.md").read_text()
    assert "terraform -chdir=infra/bootstrap apply" in readme, (
        "README 沒寫 bootstrap 步驟 —— 下一個人的第一次 plan 會以看不懂的錯誤失敗"
    )


def test_saved_plans_are_gitignored() -> None:
    """`terraform plan -out=tfplan` 產生的檔案**跟 state 一樣敏感**,而且偽裝得很好。

    它是個 zip,所以 `grep whsec_ tfplan` 什麼都找不到 —— 看起來像無害的二進位檔。
    解開之後(2026-08-18 實測)裡面有 Stripe 金鑰、PII KEK、以及帶 RDS 主密碼的
    DATABASE_URL,全部明文。

    而 `-out=tfplan` 是這個專案**刻意推薦**的做法(先審再 apply 同一份計畫,見
    RUNBOOK 情境 A 步驟 4),所以那個檔案會經常出現在工作目錄裡 —— 一次
    `git add -A` 就會把它提交上去。
    """
    ignored = (ROOT / "infra" / ".gitignore").read_text().splitlines()
    patterns = {line.strip() for line in ignored if line.strip() and not line.startswith("#")}
    assert "tfplan" in patterns or "*.tfplan" in patterns, (
        "infra/.gitignore 沒有忽略存檔的 plan —— 它含有 Stripe 金鑰與 DB 主密碼"
    )


def test_the_restore_lever_and_its_escape_hatch_stay_in_sync() -> None:
    """`ignore_changes = [snapshot_identifier]` 存在的話,RUNBOOK **必須**教 `state rm`。

    這兩件事是一組的,而拆開來看每一件都很合理 —— 那正是它危險的地方。

    `ignore_changes` 只在資源「已存在」時抑制差異。所以資源還在 state 裡的時候,
    `terraform apply -var restore_from_snapshot_identifier=X` 會被**整個忽略**,plan 回
    `No changes.` —— 事故當下你會看到 `Apply complete!` 然後以為資料回來了。
    2026-08-18 演練實測確認過這件事。

    那道 `ignore_changes` 不能拿掉:少了它,下一次**不帶** var 的 apply 會把
    `snapshot_identifier` 從有值變回 null,而那是 ForceNew → 一個空資料庫取代剛還原
    好的資料。所以正解是繞過它,而繞過的方法必須寫在還原程序裡,否則等於沒有。
    """
    if not re.search(r"ignore_changes\s*=\s*\[snapshot_identifier\]", _code(RDS_TF)):
        pytest.skip("rds.tf 沒有那道 ignore_changes,這個配對就不成立")

    runbook = (ROOT / "infra" / "RUNBOOK.md").read_text()
    assert "terraform state rm aws_db_instance.main" in runbook, (
        "RUNBOOK 沒有教 `terraform state rm` —— 少了它,情境 A 的還原指令是空操作,"
        "而且會回報成功"
    )


def test_a_migration_deploy_takes_a_restore_point_before_migrating(deploy) -> None:
    """快照那一步必須**排在 migration 之前**。

    排在後面的話它拍到的是「已經被寫壞」的狀態 —— 一個看起來完備的還原點,實際上
    還原回去問題還在。而這個錯誤不會有任何症狀,直到真的需要它。
    """
    steps = [s.get("name", "") for s in deploy["jobs"]["deploy"]["steps"]]
    snapshot = next(i for i, n in enumerate(steps) if "Snapshot the database" in n)
    migrate = next(i for i, n in enumerate(steps) if "Run database migrations" in n)

    assert snapshot < migrate, f"快照排在 migration 之後了:{steps}"

    # 淺 clone 拿不到上一次部署的 commit → 判斷不出有沒有新 migration。
    checkout = next(s for s in deploy["jobs"]["deploy"]["steps"] if "checkout" in str(s.get("uses", "")))
    assert checkout.get("with", {}).get("fetch-depth") == 0, (
        "checkout 要 fetch-depth: 0,否則無法跟上一次部署比較"
    )


def test_ci_can_create_snapshots_but_not_delete_them() -> None:
    """CD 的憑證要能建快照,**不能刪**。

    清理舊快照是人的決定(它們就是還原點)。一個能刪快照的 CI 憑證會讓「有備份」這件事
    退化成「有備份,除非哪次部署腳本寫錯」。

    也不能有 `rds:AddTagsToResource` —— CI 那邊刻意不帶 `--tags`,快照的標籤由實例的
    `copy_tags_to_snapshot` 帶過來。這是上次 autoscaling 那個「plan 綠、apply 因為缺
    Tag 權限而炸」的教訓。
    """
    assert "rds:CreateDBSnapshot" in CICD_TF
    assert "rds:DescribeDBSnapshots" in CICD_TF
    for forbidden in ("rds:DeleteDBSnapshot", "rds:DeleteDBInstance", "rds:AddTagsToResource"):
        assert forbidden not in CICD_TF, f"CD 不該有 {forbidden}"


def test_the_worker_pool_covers_its_declared_job_concurrency() -> None:
    """worker 的池子必須夠它宣告的並發 job 數用。

    `WorkerSettings.max_jobs` 是連線預算裡「worker 那一格」的依據 —— 上面那條測試信任
    它。反過來也要成立:池子小於 max_jobs 的話,同一刻到期的 cron 會卡在連線池上等,
    而那個症狀是「排程任務偶爾沒跑」,查起來完全不會指向連線池。
    """
    from app.worker import WorkerSettings

    assert _pool_per_task("worker") >= WorkerSettings.max_jobs, (
        f"worker 池子 {_pool_per_task('worker')} < max_jobs {WorkerSettings.max_jobs}"
    )


# ─ 告警接線:程式碼印的字串必須真的被過濾器接到

def _filtered_event_names() -> set[str]:
    """monitoring.tf 裡 `{ $.event = "X" }` 這種過濾器要抓的所有 X。"""
    return set(re.findall(r'pattern\s*=\s*"\{ \$\.event = \\"(\w+)\\" \}"', MONITORING_TF))


def _emitted_event_names() -> set[str]:
    """程式碼裡實際發得出來的 `event` 值。

    兩種寫法都算:`alert(..., event="x")` 的關鍵字,以及 `extra={"event": "x"}`
    的 dict 字面值。用 AST 而不是正則,免得被字串裡剛好出現的 `event=` 騙到。
    """
    names: set[str] = set()
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.keyword) and node.arg == "event":
                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    names.add(node.value.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if (
                        isinstance(key, ast.Constant) and key.value == "event"
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                    ):
                        names.add(value.value)
    return names


def test_every_log_filter_matches_an_event_the_code_actually_emits() -> None:
    """過濾器抓的 `event` 值必須真的有人發得出來。

    **這條擋的是「告警從此永遠不響」**,而「永遠不響」跟「一直很平安」在儀表板上
    長得完全一樣。舊版比對的是訊息散文(`"INVENTORY DRIFT event"`),所以一次無害的
    措辭調整就會讓告警靜默失效;改成比對 `event` 欄位之後,措辭可以自由改寫,但
    **改掉欄位值仍然會讓告警斷線** —— 所以這條測試還是必要的,只是守的東西從
    「字串長怎樣」變成「契約還在不在」。
    """
    filtered = _filtered_event_names()
    assert filtered, "monitoring.tf 裡一條 { $.event = ... } 過濾器都沒有"

    emitted = _emitted_event_names()
    orphans = filtered - emitted
    assert not orphans, (
        f"這些 event 有告警但程式碼發不出來(改名了?):{sorted(orphans)}"
    )


def test_needs_human_alarm_is_wired_to_the_field_not_to_a_phrase() -> None:
    """`needs_a_human` 必須掛在 `needs_human` 欄位上,而那個欄位只能由 alert() 產生。

    判準只有一個宣告點才不會漂:任何人加一條新的「要人來看」的 log,只要用
    `alert()` 就自動被接到;而 `needs_human` 一旦散落在各個呼叫點,下一條新的就會
    忘記帶,然後靜靜躺在 log 裡等人去 grep。
    """
    assert 'pattern = "{ $.needs_human IS TRUE }"' in MONITORING_TF, (
        "needs_a_human 的過濾器不再比對 needs_human 欄位"
    )

    # 掃 AST 而不是原始碼:註解裡提到 needs_human 是在解釋這個設計,不是在產生欄位。
    producers = {
        str(path.relative_to(ROOT))
        for path in APP.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path)))
        if (isinstance(node, ast.Constant) and node.value == "needs_human")
        or (isinstance(node, ast.keyword) and node.arg == "needs_human")
    }
    assert producers == {"app/core/logging.py"}, (
        f"needs_human 應該只由 app/core/logging.py 的 alert() 寫出,但這些檔案也碰它:"
        f"{sorted(producers - {'app/core/logging.py'})}"
    )


def test_each_service_declares_a_distinct_component() -> None:
    """三個服務的 `APP_COMPONENT` 必須各不相同。

    它同時是 Postgres 的 `application_name` 與 JSON log 的 `component`。三個都一樣的
    話,「這條連線是誰開的」跟「這行 log 是哪個 process 發的」就都失去答案 —— 而那
    正是這兩個欄位存在的唯一理由。這條測試存在是因為**它們一開始就都是一樣的**
    (env 根本沒設,全部落在程式碼的預設值上)。
    """
    values = re.findall(r'name = "APP_COMPONENT", value = "([\w-]+)"', TASKDEFS_TF)

    assert len(values) == 3, f"三個 task def 都要設 APP_COMPONENT,找到 {values}"
    assert len(set(values)) == 3, f"值必須各不相同,找到 {values}"


def test_statement_timeout_is_only_given_to_the_api() -> None:
    """`DB_STATEMENT_TIMEOUT_MS` 只能掛在 api 上。

    掛到 worker 上會砍掉對帳 cron 的長查詢 —— 更糟的是,`alembic upgrade` 是用
    worker 的 task def 跑的(deploy.yml),所以一個大表的 ALTER TABLE 會在 10 秒被
    砍掉,而部署失敗的訊息會完全指不到這個設定。
    """
    assert TASKDEFS_TF.count("api_statement_timeout_env") == 2, (
        "應該只有『宣告』與『api 引用』兩處 —— 多出來的引用表示它被掛到別的服務上了"
    )
    api_block = TASKDEFS_TF.split('resource "aws_ecs_task_definition" "api"')[1].split(
        'resource "aws_ecs_task_definition"'
    )[0]
    assert "api_statement_timeout_env" in api_block


def test_long_running_services_do_not_print() -> None:
    """常駐服務不准用 `print()` —— 那會繞過整條 JSON 管線。

    print 寫的是裸 stdout:沒有 level、沒有 trace_id、不是 JSON,所以任何以欄位為
    基礎的過濾器都看不到它。一行 print 出來的 ALERT 在 CloudWatch 上等於不存在。

    `app/scripts/` 不在此限:那些是給人在終端機跑的 CLI,stdout **就是**它們的
    介面,把報表輸出變成 JSON log 只會讓它們更難用。
    """
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if path.relative_to(APP).parts[0] == "scripts":
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, (
        "這些地方用了 print(),CloudWatch 上的欄位過濾器看不到它們:\n  "
        + "\n  ".join(offenders)
    )
