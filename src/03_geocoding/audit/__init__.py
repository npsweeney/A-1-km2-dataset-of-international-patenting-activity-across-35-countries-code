"""Manage geocoder output directories and cached continuation"""
from __future__ import annotations
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
RUN_STATE_NAME = 'run_state.json'
RUN_MANIFEST_NAME = 'run_manifest.json'

def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temporary.replace(path)

class RunGuard:
    """Protect an output directory against incompatible resume or overwrite"""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.state_path = output_dir / RUN_STATE_NAME
        self.manifest_path = output_dir / RUN_MANIFEST_NAME

    def prepare(self, identity: Mapping[str, Any], started_at_utc: str) -> None:
        if self.output_dir.exists():
            if self.manifest_path.exists():
                raise FileExistsError(f'Completed output already exists: {self.output_dir}')
            if not self.state_path.exists():
                raise FileExistsError(f'Output directory exists without a resumable run state: {self.output_dir}')
            try:
                prior = json.loads(self.state_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(f'Run state is unreadable: {self.state_path}') from error
            if prior.get('status') != 'incomplete' or prior.get('identity') != dict(identity):
                raise ValueError('Incomplete output belongs to a different input or configuration')
            return
        self.output_dir.mkdir(parents=True)
        _atomic_json(self.state_path, {'status': 'incomplete', 'started_at_utc': started_at_utc, 'identity': dict(identity), 'resume_strategy': 'replay deterministic decisions from the durable query cache'})

    def complete(self, manifest: Mapping[str, Any]) -> None:
        """Publish completion atomically, then remove the incomplete marker"""
        if not self.state_path.exists():
            raise FileNotFoundError(f'Missing incomplete run state: {self.state_path}')
        _atomic_json(self.manifest_path, manifest)
        self.state_path.unlink()
