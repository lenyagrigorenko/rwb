"""Orchestrator: requirements -> analysis -> IR -> code -> build -> verify -> docs -> report."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import yaml

from . import agents, builder, coder, recorder, runtime, validate
from .gen import backend as bgen
from .gen import docs as dgen
from .gen import frontend as fgen
from .gen import infra as igen
from .gen import mobile as mgen
from .ir import AppSpec
from .llm import LLM, Telemetry
from .profiler import profile_dir

STAGES = [
    ("intake", "Приём требований", "Аналитик"),
    ("analysis", "Анализ требований", "Аналитик"),
    ("clarify", "Уточняющие вопросы", "Аналитик"),
    ("design", "Архитектура и модель данных", "Архитектор"),
    ("validate", "Проверка чертежа", "Архитектор"),
    ("codegen", "Генерация компонентов", "Конвейер"),
    ("coding", "Бизнес-логика", "Программист"),
    ("build", "Сборка и исправление ошибок", "Программист"),
    ("verify", "Тесты и безопасность", "QA и безопасность"),
    ("docs", "Документация", "Техписатель"),
    ("publish", "Публикация", "DevOps"),
]

BEHAVIOUR_PREFIXES = ("Операция", "Событие", "Демо", "Требуется", "В требованиях", "Нет демо")


@dataclass
class RunState:
    run_id: str
    case: str
    status: str = "running"
    stages: dict = field(default_factory=lambda: {k: {"title": t, "agent": a, "status": "pending", "detail": "",
                                                      "started": None, "finished": None}
                                                  for k, t, a in STAGES})
    questions: list = field(default_factory=list)
    log: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    result: dict = field(default_factory=dict)


class Pipeline:
    def __init__(self, case_dir: Path, out_root: Path, runs_root: Path, *, run_id: str | None = None,
                 answer_timeout: float = 0, answers: Callable[[], Awaitable[dict]] | None = None,
                 skip_verify: bool = False):
        self.case_dir = case_dir
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = runs_root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state = RunState(self.run_id, case_dir.name)
        self.telemetry = Telemetry(log_path=self.run_dir / "llm_calls.jsonl")
        self.llm = LLM(self.telemetry)
        self.answer_timeout = answer_timeout
        self.answers_provider = answers
        self.skip_verify = skip_verify
        self.out_root = out_root
        self.listeners: list[Callable[[RunState], None]] = []

    # ------------------------------------------------------------------ state helpers
    def save(self) -> None:
        self.state.metrics = self.telemetry.summary()
        (self.run_dir / "state.json").write_text(json.dumps(self.state.__dict__, ensure_ascii=False, indent=1, default=str),
                                                 encoding="utf-8")
        for fn in self.listeners:
            fn(self.state)

    def log(self, text: str) -> None:
        self.state.log.append({"t": round(time.time() - self.telemetry.started, 1), "text": text})
        print(f"[{self.state.log[-1]['t']:>6}s] {text}", flush=True)
        self.save()

    def stage(self, key: str, status: str, detail: str = "") -> None:
        st = self.state.stages[key]
        st["status"] = status
        if detail:
            st["detail"] = detail
        if status == "running":
            st["started"] = time.time()
        if status in ("done", "failed", "skipped"):
            st["finished"] = time.time()
        self.save()

    # ------------------------------------------------------------------ main flow
    async def run(self) -> RunState:
        try:
            await self._run()
            self.state.status = "done"
        except Exception as e:
            self.state.status = "failed"
            self.log(f"Ошибка: {e}")
            for st in self.state.stages.values():
                if st["status"] == "running":
                    st["status"] = "failed"
            raise
        finally:
            lock = getattr(self, "_lock", None)
            if lock is not None and lock.exists() and lock.read_text().endswith(self.run_id):
                lock.unlink()
            self.save()
        return self.state

    async def _run(self) -> None:
        self.stage("intake", "running")
        requirements = (self.case_dir / "requirements.md").read_text(encoding="utf-8")
        data_dir = self.case_dir / "data"
        data_files = {p.name for p in data_dir.iterdir() if p.is_file() and not p.name.startswith(".")} if data_dir.exists() else set()
        profile = profile_dir(data_dir)
        (self.run_dir / "data_profile.md").write_text(profile, encoding="utf-8")
        self.stage("intake", "done", f"{len(requirements)} символов, файлов данных: {len(data_files)}")

        self.stage("analysis", "running")
        analysis = await agents.analyze(self.llm, requirements, profile)
        missing = [fr.id for fr in analysis.functional if not any(ac.fr == fr.id for ac in analysis.acceptance)]
        if missing:
            self.log(f"Нет критериев приёмки для {len(missing)} ФТ → дополнение")
            analysis = await agents.complete_acceptance(self.llm, requirements, analysis, missing)
        (self.run_dir / "analysis.json").write_text(analysis.model_dump_json(indent=1), encoding="utf-8")
        self.stage("analysis", "done", f"{len(analysis.functional)} ФТ, {len(analysis.acceptance)} критериев приёмки, "
                                       f"{len(analysis.ambiguities)} неоднозначностей")

        self.stage("clarify", "running")
        await self._clarify(analysis)
        self.stage("clarify", "done", f"вопросов: {len(self.state.questions)}")

        self.stage("design", "running")
        domain, behaviour = await agents.design(self.llm, requirements, profile, analysis)
        spec = self._assemble(analysis, domain, behaviour)
        self.stage("design", "done", f"{len(spec.entities)} сущностей, {len(spec.operations)} операций, "
                                     f"{len(spec.events)} событий")

        self.stage("validate", "running")
        spec = await self._validate(spec, domain, behaviour, requirements, profile, data_files)
        (self.run_dir / "spec.yaml").write_text(yaml.safe_dump(spec.model_dump(), allow_unicode=True, sort_keys=False),
                                                encoding="utf-8")

        out = self._claim_project_dir(spec.slug)
        self.state.result["project_dir"] = str(out)
        self.stage("codegen", "running")
        self._generate(spec, out, {})
        self.stage("codegen", "done", "backend, frontend, mobile, БД, OpenAPI, AsyncAPI, Docker, CI")

        self.stage("coding", "running")
        logic = await coder.write_logic(self.llm, spec)
        self.stage("coding", "done", f"методов: {len(logic)}")

        self.stage("build", "running")
        logic, build_ok, defects = await self._build_loop(spec, out, logic)
        self.state.result["build_ok"] = build_ok
        self.state.result["defects"] = defects
        self.stage("build", "done" if build_ok else "failed",
                   "сборка успешна" + (f", заглушек: {len(defects)}" if defects else ""))

        apk_task = None
        low_mem = runtime.low_memory()
        host = os.environ.get("PUBLIC_HOST", "10.0.2.2")
        api_url = f"http://{host}:{runtime.ports_for(out.name)['API_PORT']}" if host != "10.0.2.2" else None
        if spec.has_mobile and build_ok and not low_mem:
            apk_task = asyncio.create_task(builder.build_apk(out / "mobile", api_url))
            self.log("Сборка Android-приложения запущена параллельно с проверками")

        verification = {}
        if self.skip_verify:
            self.stage("verify", "skipped", "пропущено флагом")
        else:
            self.stage("verify", "running")
            verification = await self._verify(spec, out, logic)
            self.state.result["video"] = bool(verification.get("video"))
            self.stage("verify", "done", verification.get("summary", ""))

        if spec.has_mobile and build_ok and low_mem and os.environ.get("FACTORY_APK", "on") != "off":
            self.log("Мало памяти: Android-приложение собирается после проверок")
            apk_task = asyncio.create_task(builder.build_apk(out / "mobile", api_url, low_memory=True))
        if apk_task:
            apk_ok, apk, apk_log = await apk_task
            self.state.result["apk"] = str(apk) if apk_ok and apk else None
            verification["apk"] = bool(apk_ok)
            self.log("APK собран" if apk_ok else "Сборка APK не удалась")
            if not apk_ok:
                (self.run_dir / "apk.log").write_text(apk_log, encoding="utf-8")

        self.stage("docs", "running")
        dgen.generate(spec, out, run_dir=self.run_dir, verification=verification,
                      telemetry=self.telemetry.summary(), defects=defects, questions=self.state.questions,
                      result=self.state.result, spec_warnings=self.state.result.get("spec_warnings"))
        self.stage("docs", "done", "13 документов")

        self.stage("publish", "running")
        self._copy_run_artifacts(out)
        self.stage("publish", "done", str(out))

    def _claim_project_dir(self, slug: str) -> Path:
        """Project directory owned by this run; a concurrent run with the same slug gets its own copy."""
        out = self.out_root / slug
        lock = out / ".run.lock"
        if lock.exists():
            try:
                pid = int(lock.read_text().split()[0])
                os.kill(pid, 0)
                if pid != os.getpid():
                    out = self.out_root / f"{slug}-{self.run_id[-6:]}"
            except (ValueError, ProcessLookupError, PermissionError, IndexError):
                pass
        out.mkdir(parents=True, exist_ok=True)
        (out / ".run.lock").write_text(f"{os.getpid()} {self.run_id}")
        self._lock = out / ".run.lock"
        return out

    # ------------------------------------------------------------------ stages
    async def _clarify(self, analysis) -> None:
        important = [q for q in analysis.ambiguities if q.impact in ("high", "medium")][:3]
        self.state.questions = [q.model_dump() for q in important]
        self.save()
        answers: dict = {}
        if important and self.answers_provider and self.answer_timeout > 0:
            self.log(f"Вопросы заказчику: {len(important)}, ожидание ответа до {int(self.answer_timeout)} с")
            try:
                answers = await asyncio.wait_for(self.answers_provider(), self.answer_timeout)
            except asyncio.TimeoutError:
                self.log("Ответов нет — применяю ответы по умолчанию")
        for q in analysis.ambiguities:
            q.answer = answers.get(q.id) or q.default_answer
        self.state.questions = [q.model_dump() for q in important]

    def _assemble(self, analysis, domain, behaviour) -> AppSpec:
        return AppSpec(slug=domain.slug, namespace=domain.namespace, product_name=domain.product_name,
                       map_center=domain.map_center, analysis=analysis, entities=domain.entities,
                       permissions=domain.permissions, datasets=domain.datasets, integrations=domain.integrations,
                       operations=behaviour.operations, events=behaviour.events, jobs=behaviour.jobs,
                       screens=behaviour.screens, demo=behaviour.demo)

    async def _validate(self, spec, domain, behaviour, requirements, profile, data_files) -> AppSpec:
        context = agents._req_block(requirements, profile, agents.answers_text(spec.analysis))
        for attempt in range(3):
            fixes = validate.autofix(spec)
            if fixes:
                self.log("Автоисправления: " + "; ".join(fixes))
            errors = validate.check(spec, data_files, requirements)
            if not errors:
                cov = validate.coverage(spec)
                uncovered = [k for k, v in cov.items() if not v]
                self.stage("validate", "done", "чертёж корректен" + (f", без покрытия: {', '.join(uncovered)}" if uncovered else
                                                                    ", все ФТ покрыты"))
                return spec
            if attempt == 2:
                break
            self.log(f"Проверка чертежа: {len(errors)} замечаний → исправление")
            dom_err = [e for e in errors if not e.startswith(BEHAVIOUR_PREFIXES)]
            beh_err = [e for e in errors if e not in dom_err]
            tasks = []
            if dom_err:
                tasks.append(agents.repair(self.llm, "architect.domain", agents.DomainModel, domain, dom_err, context))
            if beh_err:
                beh_ctx = context + "\n\n## Модель данных (JSON)\n\n" + domain.model_dump_json(exclude={"datasets"})
                tasks.append(agents.repair(self.llm, "architect.behaviour", agents.BehaviourModel, behaviour, beh_err, beh_ctx))
            results = await asyncio.gather(*tasks)
            for r in results:
                if isinstance(r, agents.DomainModel):
                    domain = r
                else:
                    behaviour = r
            spec = self._assemble(spec.analysis, domain, behaviour)
        self.stage("validate", "done", f"остались замечания: {len(errors)}")
        self.state.result["spec_warnings"] = errors
        return spec

    def _generate(self, spec: AppSpec, out: Path, logic: dict) -> None:
        if out.exists():
            for child in out.iterdir():
                if child.name not in ("backend",):
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        out.mkdir(parents=True, exist_ok=True)
        bgen.generate(spec, out, logic, data_dir=self.case_dir / "data")
        fgen.generate(spec, out)
        igen.generate(spec, out)
        if spec.has_mobile:
            mgen.generate(spec, out)
        (out / "spec.yaml").write_text(yaml.safe_dump(spec.model_dump(), allow_unicode=True, sort_keys=False), encoding="utf-8")

    async def _build_loop(self, spec: AppSpec, out: Path, logic: dict) -> tuple[dict, bool, list[str]]:
        backend = out / "backend"
        solution = f"{spec.namespace}.slnx"
        defects: list[str] = []
        for attempt in range(3):
            bgen.generate(spec, out, logic, data_dir=self.case_dir / "data")
            (self.run_dir / f"logic_{attempt + 1}.json").write_text(json.dumps(logic, ensure_ascii=False, indent=1), encoding="utf-8")
            ok, errors, raw = await builder.build_backend(backend, solution)
            (self.run_dir / f"build_{attempt + 1}.log").write_text(
                "\n".join(f"{e.file}:{e.line} {e.code} {e.message}" for e in errors), encoding="utf-8")
            if ok:
                self.log(f"Сборка backend успешна (попытка {attempt + 1})")
                return logic, True, defects
            by_owner: dict[str, list[str]] = {}
            other: list[str] = []
            for e in errors:
                owner = coder.file_owner(spec, e.file)
                (by_owner.setdefault(owner, []) if owner else other).append(f"строка {e.line}: {e.code} {e.message}")
            self.log(f"Сборка: {len(errors)} ошибок в {len(by_owner)} методах" + (f", вне методов: {len(other)}" if other else ""))
            if other and not by_owner:
                (self.run_dir / "build.log").write_text(raw, encoding="utf-8")
                return logic, False, defects
            if attempt == 2:
                for name in by_owner:
                    logic[name] = {"body": coder.FALLBACK, "helpers": "", "usings": []}
                    defects.append(f"{name}: логика заменена заглушкой после 3 попыток сборки")
                bgen.generate(spec, out, logic, data_dir=self.case_dir / "data")
                ok, errors, raw = await builder.build_backend(backend, solution)
                return logic, ok, defects
            fixes = await asyncio.gather(*(
                coder.fix_logic(self.llm, spec, name, logic[name], self._handler_text(spec, out, name), errs)
                for name, errs in by_owner.items()))
            for name, fixed in zip(by_owner, fixes):
                logic[name] = fixed
        return logic, False, defects

    def _handler_text(self, spec: AppSpec, out: Path, name: str) -> str:
        base = out / "backend" / "src" / f"{spec.namespace}.Application"
        for p in (base / "Operations" / f"{name}.cs", base / "Jobs" / f"{name}Job.cs"):
            if p.exists():
                return p.read_text(encoding="utf-8")
        return ""

    async def _verify(self, spec: AppSpec, out: Path, logic: dict) -> dict:
        backend = out / "backend"
        ok, output = await builder.test_backend(backend, f"{spec.namespace}.slnx", "Category!=Integration")
        (self.run_dir / "tests.log").write_text(output, encoding="utf-8")
        summary = next((line.strip() for line in output.splitlines()[::-1] if "Passed!" in line or "Failed!" in line
                        or "Total tests" in line), "тесты выполнены" if ok else "есть упавшие тесты")
        result: dict = {"tests_ok": ok, "summary": summary, "demo": None}
        if not spec.demo or not spec.demo.steps:
            return result
        self.stage("verify", "running", "развёртывание стенда и прогон демо-сценария")
        deployed, log, ports = await runtime.deploy(out, spec.slug)
        if not deployed:
            self.log("Развёртывание не удалось: " + log[-300:])
            (self.run_dir / "deploy.log").write_text(log, encoding="utf-8")
            return result
        api = f"http://localhost:{ports['API_PORT']}"
        self.state.result["web_url"] = f"http://localhost:{ports['WEB_PORT']}"
        self.state.result["api_url"] = api
        best: tuple[int, dict, dict] | None = None
        for round_no in range(3):
            since = time.time() - 2
            status = await runtime.run_demo(spec, api)
            steps = status.get("steps", [])
            passed = sum(1 for st in steps if st.get("status") == "done")
            self.log(f"Демо-сценарий: {passed}/{len(steps)} шагов (раунд {round_no + 1})")
            if best is None or passed > best[0]:
                best = (passed, json.loads(json.dumps(logic)), status)
            result["demo"] = status
            targets = runtime.failed_targets(spec, status)
            targets = {k: v for k, v in targets.items() if k in logic}
            if not targets or round_no == 2:
                break
            causes = runtime.exception_summaries(await runtime.logs_since(out, since))
            self.log("Исправление по результатам прогона: " + ", ".join(targets))
            fixes = await asyncio.gather(*(
                coder.fix_logic(self.llm, spec, name, logic[name], self._handler_text(spec, out, name),
                                errs + [f"Исключение на сервере: {c}" for c in causes], runtime=True)
                for name, errs in targets.items()))
            candidate = dict(logic)
            for name, fixed in zip(targets, fixes):
                candidate[name] = fixed
            built = False
            for _ in range(3):
                bgen.generate(spec, out, candidate, data_dir=self.case_dir / "data")
                built, errors, _ = await builder.build_backend(backend, f"{spec.namespace}.slnx")
                if built:
                    break
                broken: dict[str, list[str]] = {}
                for e in errors:
                    owner = coder.file_owner(spec, e.file)
                    if owner:
                        broken.setdefault(owner, []).append(f"строка {e.line}: {e.code} {e.message}")
                if not broken:
                    break
                fixed = await asyncio.gather(*(
                    coder.fix_logic(self.llm, spec, name, candidate[name], self._handler_text(spec, out, name), errs)
                    for name, errs in broken.items()))
                candidate.update(dict(zip(broken, fixed)))
            if not built:
                self.log("Исправление не скомпилировалось — оставляю предыдущую версию")
                bgen.generate(spec, out, logic, data_dir=self.case_dir / "data")
                break
            logic.update(candidate)
            deployed, log, ports = await runtime.redeploy_clean(out, spec.slug)
            if not deployed:
                break
        if best and best[0] > sum(1 for st in (result["demo"] or {}).get("steps", []) if st.get("status") == "done"):
            self.log(f"Возврат к лучшей версии логики ({best[0]} шагов)")
            logic.clear()
            logic.update(best[1])
            result["demo"] = best[2]
            bgen.generate(spec, out, logic, data_dir=self.case_dir / "data")
            await builder.build_backend(backend, f"{spec.namespace}.slnx")
            await runtime.redeploy_clean(out, spec.slug)
        self.stage("verify", "running", "запись видео демо-сценария")
        recorded, rec_log = await recorder.record_demo(spec, out, ports["WEB_PORT"])
        result["video"] = recorded
        self.log("Видео демо-сценария записано: docs/demo.webm" if recorded else "Видео не записано")
        if not recorded:
            (self.run_dir / "recorder.log").write_text(rec_log, encoding="utf-8")
        demo = result["demo"] or {}
        done = sum(1 for st in demo.get("steps", []) if st.get("status") == "done")
        result["summary"] = f"{summary}; демо-сценарий {done}/{len(demo.get('steps', []))} шагов"
        (self.run_dir / "demo.json").write_text(json.dumps(demo, ensure_ascii=False, indent=1), encoding="utf-8")
        return result

    def _copy_run_artifacts(self, out: Path) -> None:
        dest = out / "docs" / "pipeline"
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("analysis.json", "spec.yaml", "data_profile.md", "llm_calls.jsonl", "state.json", "demo.json"):
            src = self.run_dir / name
            if src.exists():
                shutil.copy(src, dest / name)
        self.state.result["metrics"] = self.telemetry.summary()
