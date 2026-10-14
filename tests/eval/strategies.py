"""Pluggable strategy adapters for dispatch evaluation.

Each strategy takes a Scenario and produces a ScenarioResult.
Simulated strategies model known cost/latency/failure profiles.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .scorer import ScenarioResult


@dataclass
class EditSpec:
    """A single file edit operation."""
    file_path: str
    edit_type: str  # "create" | "str_replace" | "line_replace"
    old_content: str | None = None
    new_content: str = ""
    line_start: int | None = None
    line_end: int | None = None


@dataclass
class Scenario:
    """A complete eval scenario."""
    id: str
    name: str
    complexity: str
    description: str
    source_files: dict[str, str]
    edits: list[EditSpec]
    golden_output: dict[str, str]
    tags: list[str]


class DispatchStrategy(ABC):
    name: str

    @abstractmethod
    def execute(self, scenario: Scenario) -> ScenarioResult:
        ...


class EditError(Exception):
    pass


def _apply_edit(
    files: dict[str, str],
    edit: EditSpec,
    strict: bool = True,
) -> None:
    if edit.edit_type == "create":
        files[edit.file_path] = edit.new_content
    elif edit.edit_type == "str_replace":
        if edit.file_path not in files:
            if strict:
                raise EditError(f"file not found: {edit.file_path}")
            return
        content = files[edit.file_path]
        if edit.old_content and edit.old_content not in content:
            if strict:
                raise EditError(
                    f"old_content not found in {edit.file_path}: "
                    f"{edit.old_content[:60]}..."
                )
            return
        if edit.old_content:
            files[edit.file_path] = content.replace(edit.old_content, edit.new_content, 1)
        else:
            files[edit.file_path] = edit.new_content
    elif edit.edit_type == "line_replace":
        if edit.file_path not in files:
            if strict:
                raise EditError(f"file not found: {edit.file_path}")
            return
        lines = files[edit.file_path].splitlines(keepends=True)
        start = (edit.line_start or 1) - 1
        end = edit.line_end or len(lines)
        new_lines = edit.new_content.splitlines(keepends=True)
        lines[start:end] = new_lines
        files[edit.file_path] = "".join(lines)
    else:
        raise EditError(f"unknown edit_type: {edit.edit_type}")


class SimulatedSonnetWorker(DispatchStrategy):
    """Opus FO -> Sonnet worker. High reliability, moderate cost."""
    name = "sonnet_worker"
    FO_COST_PER_SCENARIO = 0.07
    WORKER_COST_PER_EDIT = 0.012
    LATENCY_PER_EDIT_S = 4.0

    def execute(self, scenario: Scenario) -> ScenarioResult:
        output_files = dict(scenario.source_files)
        attempts = 1
        error = None
        try:
            for edit in scenario.edits:
                _apply_edit(output_files, edit)
        except EditError as e:
            error = str(e)
            attempts = 2
            output_files = dict(scenario.source_files)
            for edit in scenario.edits:
                _apply_edit(output_files, edit, strict=False)
        n_edits = len(scenario.edits)
        cost = self.FO_COST_PER_SCENARIO + (self.WORKER_COST_PER_EDIT * n_edits)
        latency = self.LATENCY_PER_EDIT_S * n_edits * attempts
        return ScenarioResult(
            scenario_id=scenario.id,
            strategy=self.name,
            output_files=output_files,
            cost_usd=round(cost * attempts, 4),
            latency_s=round(latency, 2),
            attempts=attempts,
            error=error,
        )


class SimulatedHaikuWorker(DispatchStrategy):
    """Opus FO -> Haiku worker. Cheap but prone to wrong-target and punting."""
    name = "haiku_worker"
    FO_COST_PER_SCENARIO = 0.12
    WORKER_COST_PER_EDIT = 0.004
    LATENCY_PER_EDIT_S = 3.0
    PUNT_TAGS = {"similar_blocks", "ambiguous"}
    WRONG_TARGET_TAGS = {"similar_blocks"}

    def execute(self, scenario: Scenario) -> ScenarioResult:
        output_files = dict(scenario.source_files)
        attempts = 1
        error = None
        silent_wrong_target = False
        tags = set(scenario.tags)
        if tags & self.PUNT_TAGS:
            attempts = 2
            error = "haiku_punted_to_task"
        if tags & self.WRONG_TARGET_TAGS:
            silent_wrong_target = True
        try:
            for edit in scenario.edits:
                _apply_edit(output_files, edit, strict=False)
        except EditError as e:
            error = str(e)
            attempts += 1
        if silent_wrong_target and output_files:
            first_file = next(iter(output_files))
            lines = output_files[first_file].splitlines(keepends=True)
            if len(lines) > 5:
                lines[4] = "# WRONG TARGET EDIT\n"
                output_files[first_file] = "".join(lines)
        n_edits = len(scenario.edits)
        cost = self.FO_COST_PER_SCENARIO + (self.WORKER_COST_PER_EDIT * n_edits)
        return ScenarioResult(
            scenario_id=scenario.id,
            strategy=self.name,
            output_files=output_files,
            cost_usd=round(cost * attempts, 4),
            latency_s=round(self.LATENCY_PER_EDIT_S * n_edits * attempts, 2),
            attempts=attempts,
            silent_wrong_target=silent_wrong_target,
            error=error,
        )


class SimulatedScriptApply(DispatchStrategy):
    """Opus FO -> deterministic script. Zero LLM worker cost, Haiku verifies."""
    name = "script_apply"
    FO_COST_PER_SCENARIO = 0.07
    VERIFY_COST = 0.008
    SCRIPT_LATENCY_PER_EDIT_S = 0.1
    VERIFY_LATENCY_S = 2.0

    def execute(self, scenario: Scenario) -> ScenarioResult:
        output_files = dict(scenario.source_files)
        attempts = 1
        error = None
        try:
            for edit in scenario.edits:
                _apply_edit(output_files, edit)
        except EditError as e:
            error = str(e)
            attempts = 2
            output_files = dict(scenario.source_files)
            for edit in scenario.edits:
                _apply_edit(output_files, edit, strict=False)
        n_edits = len(scenario.edits)
        cost = self.FO_COST_PER_SCENARIO + self.VERIFY_COST
        latency = (self.SCRIPT_LATENCY_PER_EDIT_S * n_edits) + self.VERIFY_LATENCY_S
        return ScenarioResult(
            scenario_id=scenario.id,
            strategy=self.name,
            output_files=output_files,
            cost_usd=round(cost, 4),
            latency_s=round(latency * attempts, 2),
            attempts=attempts,
            error=error,
        )


STRATEGY_REGISTRY: dict[str, type[DispatchStrategy]] = {
    "sonnet_worker": SimulatedSonnetWorker,
    "haiku_worker": SimulatedHaikuWorker,
    "script_apply": SimulatedScriptApply,
}
