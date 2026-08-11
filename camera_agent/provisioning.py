from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hmac
import json
from pathlib import Path
import re
from typing import Any, Mapping
from urllib.parse import urljoin

import requests


PROJECT_DIR = Path(__file__).resolve().parents[1]
PROFILE_TEMPLATE_PATH = PROJECT_DIR / "mainflux" / "profile.template.json"
RULES_TEMPLATE_PATH = PROJECT_DIR / "mainflux" / "rules.template.json"
PROFILE_NAME = "Camera Agent - SenML"
DEVICE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PAGE_LIMIT = 200
REQUEST_TIMEOUT_SECONDS = 10
RULE_KINDS = ("facebook_violation", "camera_offline")


class ProvisioningError(RuntimeError):
    """A safe, operator-facing Mainflux provisioning failure."""


@dataclass(frozen=True)
class ProvisioningResult:
    device_id: str
    thing_id: str | None
    thing_key: str | None = field(repr=False)
    profile_id: str | None
    rule_ids: Mapping[str, str]
    status: str
    changes: tuple[str, ...] = ()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProvisioningError(f"Could not read provisioning template {path}: {exc}") from exc


def _render(value: Any, replacements: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _render(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, replacements) for item in value]
    if isinstance(value, str):
        rendered = value
        for placeholder, replacement in replacements.items():
            rendered = rendered.replace("{{" + placeholder + "}}", replacement)
        return rendered
    return value


def build_profile() -> dict[str, object]:
    payload = _load_json(PROFILE_TEMPLATE_PATH)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ProvisioningError("Mainflux profile template must contain exactly one profile")
    return deepcopy(payload[0])


def build_profile_create_payload() -> list[dict[str, object]]:
    """Mainflux v0.41.1 expects a bare JSON array for profile creation."""

    return [build_profile()]


def build_rules(device_id: str, thing_id: str) -> list[dict[str, object]]:
    _validate_device_id(device_id)
    if not thing_id.strip():
        raise ValueError("Mainflux Thing ID cannot be empty")
    payload = _load_json(RULES_TEMPLATE_PATH)
    rules = payload.get("rules") if isinstance(payload, dict) else None
    if not isinstance(rules, list) or len(rules) != 2:
        raise ProvisioningError("Mainflux rules template must contain exactly two rules")
    rendered = _render(
        rules,
        {"device_id": device_id, "thing_id": thing_id.strip()},
    )
    if not all(isinstance(rule, dict) for rule in rendered):
        raise ProvisioningError("Mainflux rules template contains an invalid rule")
    return rendered


def profile_is_current(profile: Mapping[str, object]) -> bool:
    return _contains(profile, build_profile())


def _validate_device_id(device_id: str) -> str:
    normalized = device_id.strip().lower()
    if not DEVICE_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            "device_id must use lowercase letters, numbers, underscores or hyphens"
        )
    return normalized


def _contains(actual: Any, desired: Any) -> bool:
    """Return whether actual contains the complete canonical desired structure."""

    if isinstance(desired, dict):
        return isinstance(actual, Mapping) and all(
            key in actual and _contains(actual[key], value)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        return isinstance(actual, list) and actual == desired
    return actual == desired


def _deep_merge(existing: Mapping[str, Any], desired: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay canonical fields while preserving unrelated server-supported data."""

    merged = deepcopy(dict(existing))
    for key, desired_value in desired.items():
        existing_value = merged.get(key)
        if isinstance(existing_value, Mapping) and isinstance(desired_value, Mapping):
            merged[key] = _deep_merge(existing_value, desired_value)
        else:
            merged[key] = deepcopy(desired_value)
    return merged


def _profile_compare_view(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize Mainflux's null-for-empty transformer serialization."""

    view = deepcopy(dict(profile))
    config = view.get("config")
    if isinstance(config, Mapping):
        config_view = deepcopy(dict(config))
        transformer = config_view.get("transformer")
        if isinstance(transformer, Mapping) and transformer.get("data_filters") is None:
            transformer_view = deepcopy(dict(transformer))
            transformer_view["data_filters"] = []
            config_view["transformer"] = transformer_view
        view["config"] = config_view
    return view


def _rule_name(device_id: str, kind: str) -> str:
    suffix = {
        "facebook_violation": "Facebook violation",
        "camera_offline": "Camera offline",
    }[kind]
    return f"Camera Agent - {device_id} - {suffix}"


def _managed_rule_prefix(device_id: str) -> str:
    return f"Managed by camera-agent for device '{device_id}'."


class MainfluxProvisioner:
    """Idempotently reconcile one Camera Agent device in Mainflux v0.41.1."""

    def __init__(
        self,
        base_url: str,
        group_id: str,
        session: requests.Session | Any | None = None,
        verify_tls: bool = True,
        token: str | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        group_id = group_id.strip()
        if not base_url:
            raise ValueError("Mainflux base URL cannot be empty")
        if not group_id:
            raise ValueError("Mainflux group ID cannot be empty")
        self.base_url = base_url
        self.group_id = group_id
        self.session = session or requests.Session()
        self.verify_tls = verify_tls
        self.token = token
        self.timeout = timeout

    def login(self, email: str, password: str) -> str:
        if not email.strip() or not password:
            raise ProvisioningError("Mainflux email and password are required")
        response = self._send(
            "POST",
            "/tokens",
            authenticated=False,
            json={"email": email.strip(), "password": password},
        )
        try:
            token = str(response.json()["token"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ProvisioningError("Mainflux login response did not contain a token") from exc
        if not token:
            raise ProvisioningError("Mainflux login response contained an empty token")
        self.token = token
        return token

    def provision_device(
        self,
        device_id: str,
        display_name: str,
        *,
        thing_id: str | None = None,
        thing_key: str | None = None,
        dry_run: bool = False,
        allow_create: bool = True,
    ) -> ProvisioningResult:
        device_id = _validate_device_id(device_id)
        display_name = display_name.strip()
        if not display_name:
            raise ValueError("display_name cannot be empty")
        thing_id = thing_id.strip() if thing_id else None
        thing_key = thing_key.strip() if thing_key else None
        self._require_token()

        changes: list[str] = []

        # Resolve identity and all existing rule conflicts before the first mutation.
        existing_thing = self._locate_thing(device_id, thing_id, thing_key)
        if existing_thing is None:
            if not dry_run and not allow_create:
                raise ProvisioningError(
                    f"Mainflux Thing for device {device_id} does not exist. "
                    "Standalone provisioning will not create it because there is no "
                    "safe local sink for the generated Thing key. Use "
                    "tools.add_device --provision-mainflux to create the device and "
                    "store its key safely."
                )
            self._preflight_orphan_rules(device_id)
            existing_rule_state = None
        else:
            existing_rule_state = self._analyze_rules(device_id, str(existing_thing["id"]))

        profile = self._reconcile_profile(dry_run=dry_run, changes=changes)
        profile_id = str(profile["id"]) if profile is not None else None

        thing = self._reconcile_thing(
            device_id=device_id,
            display_name=display_name,
            profile_id=profile_id,
            existing=existing_thing,
            supplied_key=thing_key,
            dry_run=dry_run,
            changes=changes,
        )

        if thing is None:
            # A new Thing cannot have an ID during a non-mutating dry run.
            return ProvisioningResult(
                device_id=device_id,
                thing_id=None,
                thing_key=None,
                profile_id=profile_id,
                rule_ids={},
                status="dry-run",
                changes=tuple(changes),
            )

        resolved_thing_id = str(thing["id"])
        if existing_rule_state is None:
            existing_rule_state = self._analyze_rules(device_id, resolved_thing_id)
        rule_ids = self._reconcile_rules(
            device_id,
            resolved_thing_id,
            existing_rule_state,
            dry_run=dry_run,
            changes=changes,
        )

        if dry_run:
            status = "dry-run"
        else:
            self._verify_profile(str(profile_id), profile)
            self._verify_thing(
                device_id=device_id,
                thing_id=resolved_thing_id,
                profile_id=str(profile_id),
            )
            self._verify_rules(device_id, resolved_thing_id, rule_ids)
            status = (
                "created"
                if any(change.startswith("created:") for change in changes)
                else "updated"
                if changes
                else "unchanged"
            )

        return ProvisioningResult(
            device_id=device_id,
            thing_id=resolved_thing_id,
            thing_key=str(thing.get("key") or "") or None,
            profile_id=profile_id,
            rule_ids=rule_ids,
            status=status,
            changes=tuple(changes),
        )

    def _endpoint(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _require_token(self) -> None:
        if not self.token:
            raise ProvisioningError("Mainflux login is required before provisioning")

    def _send(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        allowed_statuses: tuple[int, ...] = (),
        **kwargs: Any,
    ) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        if authenticated:
            self._require_token()
            headers["Authorization"] = f"Bearer {self.token}"
        if method.upper() in {"POST", "PUT", "PATCH"}:
            headers.setdefault("Content-Type", "application/json")
        kwargs.update(
            {
                "headers": headers,
                "timeout": self.timeout,
                "verify": self.verify_tls,
            }
        )
        try:
            request_method = getattr(self.session, "request", None)
            if request_method is not None:
                response = request_method(method.upper(), self._endpoint(path), **kwargs)
            else:
                response = getattr(self.session, method.lower())(
                    self._endpoint(path), **kwargs
                )
        except requests.RequestException as exc:
            raise ProvisioningError(
                f"Mainflux request failed for {method.upper()} {path}"
            ) from exc
        if response.status_code in allowed_statuses:
            return response
        if not bool(getattr(response, "ok", False)):
            reason = str(getattr(response, "reason", "request failed"))
            raise ProvisioningError(
                f"Mainflux rejected {method.upper()} {path}: "
                f"HTTP {response.status_code} {reason}"
            )
        return response

    def _paginate(self, path: str, collection_key: str) -> list[dict[str, Any]]:
        offset = 0
        items: list[dict[str, Any]] = []
        while True:
            response = self._send(
                "GET",
                path,
                params={"limit": PAGE_LIMIT, "offset": offset},
            )
            try:
                body = response.json()
                page = body[collection_key]
            except (KeyError, TypeError, ValueError) as exc:
                raise ProvisioningError(
                    f"Mainflux response for {path} has no {collection_key} list"
                ) from exc
            if not isinstance(page, list) or not all(isinstance(item, dict) for item in page):
                raise ProvisioningError(
                    f"Mainflux response for {path} contains invalid {collection_key}"
                )
            items.extend(deepcopy(page))
            try:
                total = int(body.get("total", len(items)))
            except (TypeError, ValueError):
                total = len(items)
            if len(items) >= total:
                break
            if not page:
                raise ProvisioningError(f"Mainflux pagination stopped early for {path}")
            try:
                returned_limit = int(body.get("limit", len(page)))
            except (TypeError, ValueError):
                returned_limit = len(page)
            offset += max(1, returned_limit, len(page))
        return items

    def _locate_thing(
        self,
        device_id: str,
        supplied_id: str | None,
        supplied_key: str | None,
    ) -> dict[str, Any] | None:
        path = f"/groups/{self.group_id}/things"
        group_things = self._paginate(path, "things")
        deterministic_name = f"camera-agent-{device_id}"
        candidate: dict[str, Any] | None = None

        if supplied_id:
            matches = [item for item in group_things if str(item.get("id")) == supplied_id]
            if len(matches) > 1:
                raise ProvisioningError(f"duplicate Mainflux Things use ID {supplied_id}")
            if matches:
                candidate = matches[0]
            else:
                response = self._send(
                    "GET",
                    f"/things/{supplied_id}",
                    allowed_statuses=(404,),
                )
                if response.status_code == 404:
                    raise ProvisioningError(f"Mainflux Thing {supplied_id} does not exist")
                candidate = response.json()
                if str(candidate.get("group_id")) != self.group_id:
                    raise ProvisioningError(
                        f"Thing {supplied_id} belongs to a different Mainflux group"
                    )

        if supplied_key:
            key_matches = [
                item
                for item in group_things
                if hmac.compare_digest(str(item.get("key") or ""), supplied_key)
            ]
            if len(key_matches) > 1:
                raise ProvisioningError("duplicate Mainflux Things use the supplied Thing key")
            key_candidate = key_matches[0] if key_matches else None
            if key_candidate is None:
                identified = self._send(
                    "POST",
                    "/identify",
                    authenticated=False,
                    allowed_statuses=(404,),
                    json={"key": supplied_key, "type": "internal"},
                )
                if identified.status_code != 404:
                    try:
                        identified_id = str(identified.json()["id"])
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ProvisioningError(
                            "Mainflux identify response did not contain a Thing ID"
                        ) from exc
                    response = self._send("GET", f"/things/{identified_id}")
                    key_candidate = response.json()
                    if str(key_candidate.get("group_id")) != self.group_id:
                        raise ProvisioningError(
                            f"Thing {identified_id} belongs to a different Mainflux group"
                        )
            if candidate is not None and key_candidate is not None:
                if str(candidate.get("id")) != str(key_candidate.get("id")):
                    raise ProvisioningError("supplied Mainflux Thing ID and key do not match")
            elif candidate is None:
                candidate = key_candidate
            if candidate is not None and not hmac.compare_digest(
                str(candidate.get("key") or ""), supplied_key
            ):
                raise ProvisioningError("supplied Mainflux Thing ID and key do not match")

        if candidate is None and not supplied_id and not supplied_key:
            managed_matches = []
            for item in group_things:
                metadata = item.get("metadata") or {}
                if not isinstance(metadata, Mapping):
                    continue
                if (
                    metadata.get("managed_by") == "camera-agent"
                    and metadata.get("device_id") == device_id
                ):
                    managed_matches.append(item)
            name_matches = [
                item for item in group_things if item.get("name") == deterministic_name
            ]
            combined = {
                str(item.get("id")): item for item in managed_matches + name_matches
            }
            if len(combined) > 1:
                raise ProvisioningError(
                    f"duplicate Mainflux Things match device_id {device_id}"
                )
            if combined:
                candidate = next(iter(combined.values()))
                metadata = candidate.get("metadata") or {}
                if not isinstance(metadata, Mapping) or (
                    metadata.get("managed_by") != "camera-agent"
                    or metadata.get("device_id") != device_id
                ):
                    raise ProvisioningError(
                        f"Thing name {deterministic_name} exists but is unmanaged"
                    )

        if candidate is None:
            return None
        if not isinstance(candidate, dict):
            raise ProvisioningError("Mainflux returned an invalid Thing")
        if str(candidate.get("group_id")) != self.group_id:
            raise ProvisioningError(
                f"Thing {candidate.get('id')} belongs to a different Mainflux group"
            )
        metadata = candidate.get("metadata") or {}
        if isinstance(metadata, Mapping):
            owner = metadata.get("managed_by")
            bound_device = metadata.get("device_id")
            if owner not in (None, "camera-agent"):
                raise ProvisioningError("selected Mainflux Thing is managed by another tool")
            if bound_device not in (None, device_id):
                raise ProvisioningError("selected Mainflux Thing belongs to another device_id")
        candidate_id = str(candidate.get("id") or "")
        identity_conflicts = []
        for item in group_things:
            if str(item.get("id") or "") == candidate_id:
                continue
            item_metadata = item.get("metadata") or {}
            same_managed_device = isinstance(item_metadata, Mapping) and (
                item_metadata.get("managed_by") == "camera-agent"
                and item_metadata.get("device_id") == device_id
            )
            if item.get("name") == deterministic_name or same_managed_device:
                identity_conflicts.append(item)
        if identity_conflicts:
            raise ProvisioningError(
                f"another Mainflux Thing already represents device_id {device_id}"
            )
        return deepcopy(candidate)

    def _reconcile_profile(
        self, *, dry_run: bool, changes: list[str]
    ) -> dict[str, Any] | None:
        desired = build_profile()
        path = f"/groups/{self.group_id}/profiles"
        profiles = self._paginate(path, "profiles")
        matches = [item for item in profiles if item.get("name") == PROFILE_NAME]
        if len(matches) > 1:
            raise ProvisioningError(f"duplicate Mainflux profiles named {PROFILE_NAME}")
        if not matches:
            changes.append("created:profile")
            if dry_run:
                return None
            response = self._send("POST", path, json=build_profile_create_payload())
            try:
                created = response.json()["profiles"]
                profile = created[0]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ProvisioningError(
                    "Mainflux profile creation response was invalid"
                ) from exc
            return deepcopy(profile)

        profile = matches[0]
        metadata = profile.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            raise ProvisioningError("existing Mainflux SenML profile has invalid metadata")
        config = profile.get("config")
        if not isinstance(config, Mapping):
            raise ProvisioningError("existing Mainflux SenML profile has invalid config")
        owner = metadata.get("managed_by")
        if owner not in (None, "camera-agent"):
            raise ProvisioningError("existing Mainflux SenML profile is unmanaged")
        if not _contains(profile, desired):
            if owner is None and not _contains(profile.get("config"), desired["config"]):
                raise ProvisioningError(
                    "existing unmanaged Mainflux SenML profile is incompatible"
                )
            changes.append("updated:profile")
            update_spec = {
                "name": desired["name"],
                "config": _deep_merge(config, desired["config"]),
                "metadata": _deep_merge(metadata, desired["metadata"]),
            }
            if not dry_run:
                self._send("PUT", f"/profiles/{profile['id']}", json=update_spec)
                profile = {**profile, **deepcopy(update_spec)}
        return deepcopy(profile)

    def _reconcile_thing(
        self,
        *,
        device_id: str,
        display_name: str,
        profile_id: str | None,
        existing: dict[str, Any] | None,
        supplied_key: str | None,
        dry_run: bool,
        changes: list[str],
    ) -> dict[str, Any] | None:
        deterministic_name = f"camera-agent-{device_id}"
        desired_metadata = {
            "managed_by": "camera-agent",
            "device_id": device_id,
            "display_name": display_name,
        }
        if existing is None:
            changes.append("created:thing")
            if dry_run:
                return None
            if profile_id is None:
                raise ProvisioningError("cannot create a Thing without a SenML profile")
            spec: dict[str, Any] = {
                "name": deterministic_name,
                "type": "device",
                "metadata": desired_metadata,
            }
            if supplied_key:
                spec["key"] = supplied_key
            response = self._send(
                "POST", f"/profiles/{profile_id}/things", json=[spec]
            )
            try:
                things = response.json()["things"]
                thing = things[0]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ProvisioningError("Mainflux Thing creation response was invalid") from exc
            return deepcopy(thing)

        thing = deepcopy(existing)
        existing_metadata = thing.get("metadata") or {}
        if not isinstance(existing_metadata, Mapping):
            raise ProvisioningError("existing Mainflux Thing has invalid metadata")
        merged_metadata = {**existing_metadata, **desired_metadata}
        if not thing.get("key"):
            raise ProvisioningError("existing Mainflux Thing response did not contain its key")
        if (
            thing.get("name") != deterministic_name
            or thing.get("type") != "device"
            or dict(existing_metadata) != merged_metadata
        ):
            changes.append("updated:thing")
            if not dry_run:
                self._send(
                    "PUT",
                    f"/things/{thing['id']}",
                    json={
                        "key": thing["key"],
                        "name": deterministic_name,
                        "type": "device",
                        "metadata": merged_metadata,
                    },
                )
            thing.update(
                {
                    "name": deterministic_name,
                    "type": "device",
                    "metadata": merged_metadata,
                }
            )
        if profile_id is not None and str(thing.get("profile_id")) != profile_id:
            changes.append("updated:thing-profile")
            if not dry_run:
                self._send(
                    "PATCH",
                    f"/things/{thing['id']}",
                    json={"profile_id": profile_id, "group_id": self.group_id},
                )
            thing["profile_id"] = profile_id
        return thing

    def _list_rules(self) -> list[dict[str, Any]]:
        path = f"/svcrules/groups/{self.group_id}/rules"
        return self._paginate(path, "rules")

    @staticmethod
    def _rules_by_name(
        rules: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for rule in rules:
            by_name.setdefault(str(rule.get("name") or ""), []).append(rule)
        return by_name

    def _rule_assignments(self, rule: Mapping[str, Any]) -> set[str]:
        rule_id = str(rule.get("id") or "")
        rule_name = str(rule.get("name") or rule_id or "unnamed")
        if not rule_id:
            raise ProvisioningError(
                f"Mainflux rule {rule_name} has no ID; cannot verify assignments"
            )
        response = self._send("GET", f"/svcrules/rules/{rule_id}/things")
        context = (
            f"status={getattr(response, 'status_code', '?')}, "
            f"content_type={getattr(response, 'headers', {}).get('content-type', '?')}, "
            f"bytes={len(getattr(response, 'content', b'') or b'')}"
        )
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise ProvisioningError(
                f"Mainflux rule {rule_name} assignment response was invalid ({context})"
            ) from exc
        if isinstance(payload, Mapping):
            raw_assignments = payload.get("thing_ids")
            if raw_assignments is None and "thing_ids" in payload:
                raw_assignments = []
            if raw_assignments is None:
                raw_assignments = payload.get("things", payload.get("ids"))
        else:
            raw_assignments = payload
        if not isinstance(raw_assignments, list):
            fields = ",".join(sorted(payload.keys())) if isinstance(payload, Mapping) else type(payload).__name__
            raise ProvisioningError(
                f"Mainflux rule {rule_name} assignment response was invalid "
                f"({context}; fields={fields})"
            )
        assignments: set[str] = set()
        for item in raw_assignments:
            if isinstance(item, Mapping):
                item = item.get("id")
            if item is not None and str(item).strip():
                assignments.add(str(item))
        return assignments

    def _preflight_orphan_rules(self, device_id: str) -> None:
        by_name = self._rules_by_name(self._list_rules())
        for kind in RULE_KINDS:
            name = _rule_name(device_id, kind)
            matches = by_name.get(name, [])
            if len(matches) > 1:
                raise ProvisioningError(f"duplicate Mainflux rules named {name}")
            if matches:
                description = str(matches[0].get("description") or "")
                if not description.startswith(_managed_rule_prefix(device_id)):
                    raise ProvisioningError(f"Mainflux rule {name} is unmanaged")
                raise ProvisioningError(
                    f"managed Mainflux rule {name} exists without a matching Thing"
                )

    def _analyze_rules(
        self, device_id: str, thing_id: str
    ) -> dict[str, tuple[dict[str, Any] | None, set[str]]]:
        rules = self._list_rules()
        by_name = self._rules_by_name(rules)
        expected_names = {_rule_name(device_id, kind) for kind in RULE_KINDS}
        assignments_by_id: dict[str, set[str]] = {}
        for rule in rules:
            rule_id = str(rule.get("id") or "")
            assignments = self._rule_assignments(rule)
            assignments_by_id[rule_id] = assignments
            rule_name = str(rule.get("name") or "unnamed")
            if rule_name not in expected_names and thing_id in assignments:
                raise ProvisioningError(
                    f"legacy or foreign Mainflux rule '{rule_name}' is assigned to "
                    f"device {device_id}; explicitly migrate or unassign that Thing "
                    "before provisioning per-device rules. No assignment was changed."
                )
        state: dict[str, tuple[dict[str, Any] | None, set[str]]] = {}
        for kind, desired in zip(RULE_KINDS, build_rules(device_id, thing_id)):
            name = str(desired["name"])
            matches = by_name.get(name, [])
            if len(matches) > 1:
                raise ProvisioningError(f"duplicate Mainflux rules named {name}")
            if not matches:
                state[kind] = (None, set())
                continue
            existing = matches[0]
            description = str(existing.get("description") or "")
            if not description.startswith(_managed_rule_prefix(device_id)):
                raise ProvisioningError(f"Mainflux rule {name} is unmanaged")
            rule_id = str(existing.get("id") or "")
            if not rule_id:
                raise ProvisioningError(f"Mainflux rule {name} has no ID")
            assignments = assignments_by_id[rule_id]
            extra = assignments - {thing_id}
            if extra:
                raise ProvisioningError(
                    f"Mainflux rule {name} has an extra Thing assignment"
                )
            state[kind] = (existing, assignments)
        return state

    def _reconcile_rules(
        self,
        device_id: str,
        thing_id: str,
        state: dict[str, tuple[dict[str, Any] | None, set[str]]],
        *,
        dry_run: bool,
        changes: list[str],
    ) -> dict[str, str]:
        desired_rules = dict(zip(RULE_KINDS, build_rules(device_id, thing_id)))
        missing = [desired_rules[kind] for kind in RULE_KINDS if state[kind][0] is None]
        if missing:
            for kind in RULE_KINDS:
                if state[kind][0] is None:
                    changes.append(f"created:rule:{kind}")
            if not dry_run:
                self._send(
                    "POST",
                    f"/svcrules/groups/{self.group_id}/rules",
                    json={"rules": missing},
                )

        for kind in RULE_KINDS:
            existing, assignments = state[kind]
            if existing is None:
                continue
            desired = desired_rules[kind]
            if self._rule_needs_update(existing, desired):
                changes.append(f"updated:rule:{kind}")
                if not dry_run:
                    self._send(
                        "PUT",
                        f"/svcrules/rules/{existing['id']}",
                        json=self._rule_update_payload(desired),
                    )
            if thing_id not in assignments:
                changes.append(f"assigned:rule:{kind}")
                if not dry_run:
                    self._send(
                        "POST",
                        f"/svcrules/rules/{existing['id']}/things",
                        json={"thing_ids": [thing_id]},
                    )

        if dry_run:
            return {
                kind: str(existing["id"])
                for kind, (existing, _) in state.items()
                if existing is not None
            }

        verified_state = self._analyze_rules(device_id, thing_id)
        rule_ids: dict[str, str] = {}
        for kind in RULE_KINDS:
            existing, assignments = verified_state[kind]
            if existing is None or assignments != {thing_id}:
                raise ProvisioningError(
                    f"Mainflux rule {_rule_name(device_id, kind)} failed verification"
                )
            desired = desired_rules[kind]
            if self._rule_needs_update(existing, desired):
                raise ProvisioningError(
                    f"Mainflux rule {_rule_name(device_id, kind)} failed semantic verification"
                )
            rule_ids[kind] = str(existing["id"])
        return rule_ids

    @staticmethod
    def _rule_needs_update(
        existing: Mapping[str, Any], desired: Mapping[str, Any]
    ) -> bool:
        existing_input = existing.get("input") or {}
        desired_input = desired.get("input") or {}
        return any(
            (
                existing.get("name") != desired.get("name"),
                existing.get("description") != desired.get("description"),
                not isinstance(existing_input, Mapping)
                or existing_input.get("type") != desired_input.get("type"),
                existing.get("conditions") != desired.get("conditions"),
                str(existing.get("operator") or "")
                != str(desired.get("operator") or ""),
                MainfluxProvisioner._canonical_actions(existing.get("actions"))
                != MainfluxProvisioner._canonical_actions(desired.get("actions")),
            )
        )

    @staticmethod
    def _canonical_actions(actions: Any) -> Any:
        """Ignore empty server-default fields without hiding meaningful drift."""

        if not isinstance(actions, list):
            return actions
        canonical: list[Any] = []
        for action in actions:
            if not isinstance(action, Mapping):
                canonical.append(action)
                continue
            item = dict(action)
            if item.get("id") in (None, ""):
                item.pop("id", None)
            canonical.append(item)
        return canonical

    @staticmethod
    def _rule_update_payload(desired: Mapping[str, Any]) -> dict[str, Any]:
        desired_input = desired.get("input") or {}
        return {
            "name": desired["name"],
            "description": desired.get("description", ""),
            "input": {"type": desired_input["type"]},
            "conditions": deepcopy(desired["conditions"]),
            "operator": desired.get("operator", ""),
            "actions": deepcopy(desired["actions"]),
        }

    def _verify_thing(self, *, device_id: str, thing_id: str, profile_id: str) -> None:
        response = self._send("GET", f"/things/{thing_id}")
        try:
            thing = response.json()
        except (TypeError, ValueError) as exc:
            raise ProvisioningError("Mainflux Thing verification response was invalid") from exc
        metadata = thing.get("metadata") or {}
        if (
            str(thing.get("group_id")) != self.group_id
            or str(thing.get("profile_id")) != profile_id
            or thing.get("name") != f"camera-agent-{device_id}"
            or not isinstance(metadata, Mapping)
            or metadata.get("managed_by") != "camera-agent"
            or metadata.get("device_id") != device_id
        ):
            raise ProvisioningError(f"Mainflux Thing {thing_id} failed verification")

    def _verify_profile(
        self, profile_id: str, expected_profile: Mapping[str, Any]
    ) -> None:
        profiles = self._paginate(
            f"/groups/{self.group_id}/profiles", "profiles"
        )
        matches = [item for item in profiles if str(item.get("id")) == profile_id]
        expected = {
            "name": expected_profile.get("name"),
            "config": expected_profile.get("config"),
            "metadata": expected_profile.get("metadata"),
        }
        actual = _profile_compare_view(matches[0]) if len(matches) == 1 else {}
        if (
            len(matches) != 1
            or not profile_is_current(actual)
            or not _contains(actual, expected)
        ):
            actual_config = actual.get("config") if isinstance(actual, Mapping) else None
            actual_metadata = actual.get("metadata") if isinstance(actual, Mapping) else None
            differences: list[str] = []
            if isinstance(actual_config, Mapping):
                for key, wanted in (expected.get("config") or {}).items():
                    got = actual_config.get(key)
                    if got != wanted:
                        differences.append(f"config.{key}={got!r} expected={wanted!r}")
            if isinstance(actual_metadata, Mapping):
                for key, wanted in (expected.get("metadata") or {}).items():
                    got = actual_metadata.get(key)
                    if got != wanted:
                        differences.append(f"metadata.{key}={got!r} expected={wanted!r}")
            raise ProvisioningError(
                f"Mainflux SenML profile {profile_id} failed verification "
                f"(matches={len(matches)}, config_fields="
                f"{','.join(sorted(actual_config)) if isinstance(actual_config, Mapping) else type(actual_config).__name__}, "
                f"metadata_fields="
                f"{','.join(sorted(actual_metadata)) if isinstance(actual_metadata, Mapping) else type(actual_metadata).__name__}, "
                f"differences={' | '.join(differences[:8]) or 'nested/unknown'})"
            )

    def _verify_rules(
        self, device_id: str, thing_id: str, expected_ids: Mapping[str, str]
    ) -> None:
        state = self._analyze_rules(device_id, thing_id)
        desired_rules = dict(zip(RULE_KINDS, build_rules(device_id, thing_id)))
        for kind in RULE_KINDS:
            existing, assignments = state[kind]
            if (
                existing is None
                or str(existing.get("id")) != expected_ids.get(kind)
                or assignments != {thing_id}
                or self._rule_needs_update(existing, desired_rules[kind])
            ):
                raise ProvisioningError(
                    f"Mainflux rule {_rule_name(device_id, kind)} failed final verification"
                )


__all__ = [
    "MainfluxProvisioner",
    "PROFILE_NAME",
    "ProvisioningError",
    "ProvisioningResult",
    "build_profile",
    "build_profile_create_payload",
    "build_rules",
    "profile_is_current",
]
