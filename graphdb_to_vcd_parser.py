import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

import requests
import yaml
import vcd.core as core
import vcd.types as types
from vcd.core import VCD, ElementType, SetMode


def get_local_name(uri: str) -> str:
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.rsplit("/", 1)[-1]


def safe_name(text: str) -> str:
    return "".join(c if c.isalnum() or c in ["-", "_"] else "_" for c in text)


class SparqlClient:
    def __init__(self, endpoint_url: str) -> None:
        self.endpoint_url = endpoint_url

    def query(self, sparql_query: str) -> Optional[dict]:
        params = {"query": sparql_query}
        headers = {"Accept": "application/sparql-results+json"}
        try:
            response = requests.get(self.endpoint_url, headers=headers, params=params, timeout=30)
            if response.status_code == 200:
                return response.json()
            print(f"[ERROR] HTTP {response.status_code}")
            print(f"[ERROR] Response: {response.text}")
            return None
        except Exception as exc:
            print(f"[ERROR] SPARQL query failed: {exc}")
            return None


class ActionsProcessor:
    def __init__(self, sparql_results: dict) -> None:
        self.sparql_results = sparql_results

    def get_data_by_action(self) -> dict:
        data_by_action: Dict[str, dict] = {}
        if not self.sparql_results:
            return data_by_action

        for b in self.sparql_results["results"]["bindings"]:
            action_uri = b["action"]["value"]
            scene_uri = b["scene"]["value"]
            type_uri = b.get("type", {}).get("value", "")
            start_str = b.get("start", {}).get("value", None)
            end_str = b.get("end", {}).get("value", None)

            action_id = get_local_name(action_uri)
            scene_name = get_local_name(scene_uri)
            sem_type = get_local_name(type_uri) if type_uri else "UnknownAction"

            start_val = int(start_str) if start_str else 0
            end_val = int(end_str) if end_str else start_val

            if action_id not in data_by_action:
                data_by_action[action_id] = {
                    "scene_name": scene_name,
                    "semantic_type": sem_type,
                    "start_framestamp": start_val,
                    "end_framestamp": end_val,
                }
            else:
                if start_val < data_by_action[action_id]["start_framestamp"]:
                    data_by_action[action_id]["start_framestamp"] = start_val
                if end_val > data_by_action[action_id]["end_framestamp"]:
                    data_by_action[action_id]["end_framestamp"] = end_val

        return data_by_action


class EventsProcessor:
    def __init__(self, sparql_results: dict) -> None:
        self.sparql_results = sparql_results

    def get_data_by_event(self) -> dict:
        data_by_event: Dict[str, dict] = {}
        if not self.sparql_results:
            return data_by_event

        for b in self.sparql_results["results"]["bindings"]:
            event_uri = b["event"]["value"]
            scene_uri = b["scene"]["value"]
            type_uri = b.get("type", {}).get("value", "")
            framestamp_str = b.get("framestamp", {}).get("value", None)

            event_id = get_local_name(event_uri)
            scene_name = get_local_name(scene_uri)
            sem_type = get_local_name(type_uri) if type_uri else "UnknownEvent"
            f_val = int(framestamp_str) if framestamp_str else 0

            if event_id not in data_by_event:
                data_by_event[event_id] = {
                    "scene_name": scene_name,
                    "semantic_type": sem_type,
                    "framestamp": f_val,
                }
            else:
                data_by_event[event_id]["framestamp"] = f_val
                data_by_event[event_id]["semantic_type"] = sem_type

        return data_by_event


class RelationshipsProcessor:
    def __init__(self, sparql_results: dict) -> None:
        self.sparql_results = sparql_results

    def get_data_by_relation(self) -> dict:
        data_by_rel: Dict[str, dict] = {}
        if not self.sparql_results:
            return data_by_rel

        for b in self.sparql_results["results"]["bindings"]:
            scene_uri = b.get("scene", {}).get("value")
            if not scene_uri:
                continue

            rel_type_uri = b["relType"]["value"]
            if rel_type_uri.endswith("hasAction") or rel_type_uri.endswith("hasEvent"):
                continue

            subject_uri = b["subject"]["value"]
            object_uri = b["object"]["value"]

            sub_id = get_local_name(subject_uri)
            obj_id = get_local_name(object_uri)
            if sub_id.startswith("scene-") or obj_id.startswith("scene-"):
                continue

            rel_type = get_local_name(rel_type_uri)
            scene_name = get_local_name(scene_uri)

            rel_id = f"{sub_id}{rel_type}{obj_id}"
            if rel_id not in data_by_rel:
                data_by_rel[rel_id] = {
                    "scene_name": scene_name,
                    "semantic_type": rel_type,
                    "subject": {"name": sub_id},
                    "object": {"name": obj_id},
                }

        return data_by_rel


class HypotenuseProcessor:
    def __init__(self, sparql_results: dict) -> None:
        self.sparql_results = sparql_results

    def get_data_by_scene(self) -> dict:
        data_by_scene: Dict[str, list] = {}
        if not self.sparql_results:
            return data_by_scene

        for b in self.sparql_results["results"]["bindings"]:
            scene_uri = b["scene"]["value"]
            veh_uri = b["vehiculo"]["value"]
            frame_uri = b["frame"]["value"]
            hyp_str = b["hypotenuse"]["value"]

            scene_name = get_local_name(scene_uri)
            obj_name = get_local_name(veh_uri)
            frame_local = get_local_name(frame_uri)

            if "_frame_" not in frame_local:
                continue
            try:
                frame_int = int(frame_local.rsplit("_frame_", 1)[-1])
            except ValueError:
                continue

            hyp_val = float(hyp_str)

            data_by_scene.setdefault(scene_name, []).append(
                {"object_name": obj_name, "framestamp": frame_int, "hypotenuse": hyp_val}
            )

        return data_by_scene


class VcdEventsActionsInserter:
    def __init__(
        self,
        data_by_action: dict,
        data_by_event: dict,
        data_by_relation: dict,
        data_by_hypotenuse: dict,
        thresholds: dict,
        input_dir: str,
        output_dir: str,
    ) -> None:
        self.data_by_action = data_by_action
        self.data_by_event = data_by_event
        self.data_by_relation = data_by_relation
        self.data_by_hypotenuse = data_by_hypotenuse
        self.thresholds = thresholds
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.action_names = set(self.data_by_action.keys())
        self.event_names = set(self.data_by_event.keys())
        self.scenes_with_data = {
            info["scene_name"] for info in self.data_by_action.values()
        }
        self.scenes_with_data.update(info["scene_name"] for info in self.data_by_event.values())
        self.scenes_with_data.update(info["scene_name"] for info in self.data_by_relation.values())
        self.scenes_with_data.update(self.data_by_hypotenuse.keys())

        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir, exist_ok=True)

    def run(self) -> None:
        files = [f for f in os.listdir(self.input_dir) if f.lower().endswith(".json")]
        for file_name in files:
            input_path = os.path.join(self.input_dir, file_name)
            out_path = os.path.join(self.output_dir, file_name)
            scene_from_name = Path(file_name).stem
            # Fast path: if the scene isn't in any result set, just copy the file.
            if scene_from_name not in self.scenes_with_data:
                shutil.copy2(input_path, out_path)
                continue
            vcd_obj = VCD()
            try:
                vcd_obj.load_from_file(input_path)
            except Exception as exc:
                print(f"[ERROR] Loading '{input_path}': {exc}")
                continue

            scene_name = self._get_scene_name_from_metadata(vcd_obj)
            if not scene_name:
                print(f"[INFO] '{file_name}' without scene_name, copying.")
                shutil.copy2(input_path, out_path)
                continue

            if scene_name not in self.scenes_with_data:
                shutil.copy2(input_path, out_path)
                continue

            a_cnt = self._insert_actions_for_scene(vcd_obj, scene_name)
            e_cnt = self._insert_events_for_scene(vcd_obj, scene_name)
            r_cnt = self._insert_relations_for_scene(vcd_obj, scene_name)
            h_cnt = self._insert_hypotenuse_for_scene(vcd_obj, scene_name)

            total = a_cnt + e_cnt + r_cnt + h_cnt
            if total > 0:
                try:
                    vcd_obj.save(out_path)
                    print(f"[OK] '{file_name}' -> '{out_path}' | A={a_cnt}, E={e_cnt}, R={r_cnt}, H={h_cnt}")
                except Exception as exc:
                    print(f"[ERROR] Saving '{out_path}': {exc}")
            else:
                shutil.copy2(input_path, out_path)

    def _get_scene_name_from_metadata(self, vcd_obj: VCD) -> str:
        return (vcd_obj.get_metadata() or {}).get("scene_name", "")

    def _find_action_uid(self, vcd_obj: VCD, name: str) -> Optional[str]:
        for uid in vcd_obj.get_actions() or []:
            action = vcd_obj.get_action(uid)
            if action and action.get("name") == name:
                return uid
        return None

    def _find_event_uid(self, vcd_obj: VCD, name: str) -> Optional[str]:
        for uid in vcd_obj.get_events() or []:
            event = vcd_obj.get_event(uid)
            if event and event.get("name") == name:
                return uid
        return None

    def _parse_object_parts(self, identifier: str, sem_type: str, scene_name: str) -> List[str]:
        base = re.sub(r"-?frames_\d+_\d+$", "", identifier)
        base = re.sub(r"-?frame_\d+$", "", base)

        scene_safe = safe_name(scene_name)
        prefix = f"{sem_type}-{scene_safe}-"
        if base.startswith(prefix):
            base = base[len(prefix) :]
        elif base.startswith(f"{sem_type}-"):
            base = base[len(sem_type) + 1 :]

        if not base:
            return []
        return base.split("-")

    def _resolve_element_type(self, name: str) -> ElementType:
        if name in self.action_names:
            return ElementType.action
        if name in self.event_names:
            return ElementType.event
        return ElementType.object

    def _resolve_uid(self, vcd_obj: VCD, name: str) -> Optional[str]:
        if name in self.action_names:
            return self._find_action_uid(vcd_obj, name)
        if name in self.event_names:
            return self._find_event_uid(vcd_obj, name)
        return vcd_obj.get_object_uid_by_name(name)

    def _insert_relations_for_scene(self, vcd_obj: VCD, scene_name: str) -> int:
        inserted = 0
        existing = vcd_obj.get_relations() or {}
        rel_counter = len(existing)

        for rel_id, info in self.data_by_relation.items():
            if info["scene_name"] != scene_name:
                continue
            sem_type = info["semantic_type"]
            sub_name = info["subject"]["name"]
            obj_name = info["object"]["name"]

            sub_uid = self._resolve_uid(vcd_obj, sub_name)
            obj_uid = self._resolve_uid(vcd_obj, obj_name)
            if sub_uid is None or obj_uid is None:
                print(f"[WARN] Relation '{rel_id}' missing UID: sub={sub_name}, obj={obj_name}")
                continue

            f_start, f_end = self._get_relation_frames(vcd_obj, sub_name, obj_name)
            name = f"Relation{rel_counter}"
            rel_counter += 1
            vcd_obj.add_relation_subject_object(
                name=name,
                semantic_type=sem_type,
                subject_type=self._resolve_element_type(sub_name),
                subject_uid=sub_uid,
                object_type=self._resolve_element_type(obj_name),
                object_uid=obj_uid,
                frame_value=(f_start, f_end),
                set_mode=SetMode.union,
            )
            inserted += 1
        return inserted

    def _insert_hypotenuse_for_scene(self, vcd_obj: VCD, scene_name: str) -> int:
        if scene_name not in self.data_by_hypotenuse:
            return 0

        inserted = 0
        items = self.data_by_hypotenuse[scene_name]

        for item in items:
            obj_name = item["object_name"]
            frame_val = item["framestamp"]
            hyp_val = item["hypotenuse"]

            obj_uid = vcd_obj.get_object_uid_by_name(obj_name)
            if obj_uid is None:
                continue

            vcd_obj.add_object_data(
                uid=obj_uid,
                object_data=types.num("hypotenuse", hyp_val),
                frame_value=frame_val,
                set_mode=core.SetMode.union,
            )
            inserted += 1

        return inserted

    def _insert_actions_for_scene(self, vcd_obj: VCD, scene_name: str) -> int:
        inserted = 0
        for action_id, info in self.data_by_action.items():
            if info["scene_name"] != scene_name:
                continue
            st, en = info["start_framestamp"], info["end_framestamp"]
            sem_type = info["semantic_type"]

            vcd_obj.add_action(
                name=action_id,
                semantic_type=sem_type,
                frame_value=(st, en),
                uid=None,
                set_mode=SetMode.union,
            )
            inserted += 1

            parts = self._parse_object_parts(action_id, sem_type, scene_name)
            uid = self._find_action_uid(vcd_obj, action_id)
            if uid is None:
                continue

            if sem_type == "BrakingHard" and len(parts) >= 1:
                brake_val = self.thresholds.get("brake")
                if brake_val is not None:
                    vcd_obj.add_action_data(
                        uid=uid,
                        action_data=types.num("threshold_braking_value", brake_val),
                        frame_value=(st, en),
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("vehicle", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

            if sem_type == "BrakingHardWithPedestrianCrossing" and len(parts) >= 3:
                brake_val = self.thresholds.get("brake")
                if brake_val is not None:
                    vcd_obj.add_action_data(
                        uid=uid,
                        action_data=types.num("threshold_braking_value", brake_val),
                        frame_value=(st, en),
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("vehicle", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("pedestrian", parts[1]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("lane", parts[2]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

            if sem_type == "AceleratingHard" and len(parts) >= 1:
                accel_val = self.thresholds.get("acceleration")
                if accel_val is not None:
                    vcd_obj.add_action_data(
                        uid=uid,
                        action_data=types.num("threshold_acceleration_value", accel_val),
                        frame_value=(st, en),
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("vehicle", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

            if sem_type == "PedestrianCrossingLane" and len(parts) >= 2:
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("pedestrian", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("lane", parts[1]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

            if sem_type == "Following" and len(parts) >= 2:
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("followed_car", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("following_car", parts[1]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

            if sem_type == "ChangingLane" and len(parts) >= 3:
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("vehicle", parts[2]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("FromLane", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("ToLane", parts[1]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

            if sem_type == "CuttingIn" and len(parts) >= 4:
                threshold_val = self.thresholds.get("cut_in_distance_to_ego")
                if threshold_val is not None:
                    vcd_obj.add_action_data(
                        uid=uid,
                        action_data=types.num("threshold_cutting_in_value", threshold_val),
                        frame_value=(st, en),
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("affected_vehicle", parts[3]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("cutting_in_vehicle", parts[2]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("FromLane", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("ToLane", parts[1]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

            if sem_type == "CuttingOut" and len(parts) >= 4:
                threshold_val = self.thresholds.get("cut_out_distance_to_ego")
                if threshold_val is not None:
                    vcd_obj.add_action_data(
                        uid=uid,
                        action_data=types.num("threshold_cutting_out_value", threshold_val),
                        frame_value=(st, en),
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("affected_vehicle", parts[3]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("cutting_out_vehicle", parts[2]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("FromLane", parts[0]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )
                vcd_obj.add_action_data(
                    uid=uid,
                    action_data=types.text("ToLane", parts[1]),
                    frame_value=(st, en),
                    set_mode=SetMode.union,
                )

        return inserted

    def _insert_events_for_scene(self, vcd_obj: VCD, scene_name: str) -> int:
        inserted = 0
        for event_id, info in self.data_by_event.items():
            if info["scene_name"] != scene_name:
                continue
            frm = info["framestamp"]
            sem_type = info["semantic_type"]

            vcd_obj.add_event(
                name=event_id,
                semantic_type=sem_type,
                frame_value=(frm, frm),
                uid=None,
                set_mode=SetMode.union,
            )
            inserted += 1

            parts = self._parse_object_parts(event_id, sem_type, scene_name)
            uid = self._find_event_uid(vcd_obj, event_id)
            if uid is None:
                continue

            if sem_type == "HardBrake" and len(parts) >= 1:
                brake_val = self.thresholds.get("brake")
                if brake_val is not None:
                    vcd_obj.add_event_data(
                        uid=uid,
                        event_data=types.num("threshold_braking_value", brake_val),
                        frame_value=frm,
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("vehicle", parts[0]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )

            if sem_type == "HardBrakeWithPedestrianCrossing" and len(parts) >= 3:
                brake_val = self.thresholds.get("brake")
                if brake_val is not None:
                    vcd_obj.add_event_data(
                        uid=uid,
                        event_data=types.num("threshold_braking_value", brake_val),
                        frame_value=frm,
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("vehicle", parts[0]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("pedestrian", parts[1]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("lane", parts[2]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )

            if sem_type == "HardAcceleration" and len(parts) >= 1:
                accel_val = self.thresholds.get("acceleration")
                if accel_val is not None:
                    vcd_obj.add_event_data(
                        uid=uid,
                        event_data=types.num("threshold_acceleration_value", accel_val),
                        frame_value=frm,
                        set_mode=SetMode.union,
                    )
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("vehicle", parts[0]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )

            if sem_type == "PedestrianOnLane" and len(parts) >= 2:
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("pedestrian", parts[0]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("lane", parts[1]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )

            if sem_type == "Follows" and len(parts) >= 2:
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("followed_car", parts[0]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )
                vcd_obj.add_event_data(
                    uid=uid,
                    event_data=types.text("following_car", parts[1]),
                    frame_value=frm,
                    set_mode=SetMode.union,
                )

        return inserted

    def _get_relation_frames(self, vcd_obj: VCD, sub_name: str, obj_name: str) -> tuple:
        if sub_name in self.action_names:
            act_uid = self._find_action_uid(vcd_obj, sub_name)
            if act_uid is not None:
                act = vcd_obj.get_action(act_uid)
                fi = act.get("frame_intervals", []) if act else []
                if fi:
                    return (fi[0]["frame_start"], fi[0]["frame_end"])
            return (0, 0)

        if sub_name in self.event_names:
            ev_uid = self._find_event_uid(vcd_obj, sub_name)
            if ev_uid is not None:
                ev = vcd_obj.get_event(ev_uid)
                fi = ev.get("frame_intervals", []) if ev else []
                if fi:
                    return (fi[0]["frame_start"], fi[0]["frame_end"])
            return (0, 0)

        if obj_name in self.action_names:
            act_uid = self._find_action_uid(vcd_obj, obj_name)
            if act_uid is not None:
                act = vcd_obj.get_action(act_uid)
                fi = act.get("frame_intervals", []) if act else []
                if fi:
                    return (fi[0]["frame_start"], fi[0]["frame_end"])
            return (0, 0)

        if obj_name in self.event_names:
            ev_uid = self._find_event_uid(vcd_obj, obj_name)
            if ev_uid is not None:
                ev = vcd_obj.get_event(ev_uid)
                fi = ev.get("frame_intervals", []) if ev else []
                if fi:
                    return (fi[0]["frame_start"], fi[0]["frame_end"])
            return (0, 0)

        return (0, 0)


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def resolve_path(root: Path, value: str) -> str:
    p = Path(value)
    return str(p if p.is_absolute() else (root / p))


def main() -> None:
    config_path = Path(__file__).resolve().parent / "conf.yaml"
    if not config_path.exists():
        print(f"[ERROR] Config not found: {config_path}")
        return

    config = load_config(config_path)
    thresholds = config.get("thresholds", {})
    endpoint_url = config.get("endpoint_url")
    if not endpoint_url:
        db_cfg = config.get("database", {})
        db_ip = db_cfg.get("db_ip")
        db_port = db_cfg.get("db_port")
        repo = db_cfg.get("repository")
        if db_ip and db_port and repo:
            endpoint_url = f"http://{db_ip}:{db_port}/repositories/{repo}"
    if not endpoint_url:
        print("[ERROR] endpoint_url not configured in conf.yaml")
        return

    cfg_root = config_path.parent
    input_dir = resolve_path(cfg_root, config["vcd"]["vcd_path"])
    output_dir_value = config.get("paths", {}).get("output_dir", "./vcds_pruebas2_enriched")
    output_dir = resolve_path(cfg_root, output_dir_value)

    prefix_alias = config["ontology"]["pref"]
    ontology_uri = config["ontology"]["my_uri"]

    client = SparqlClient(endpoint_url)

    sparql_query_actions = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX owl: <http://www.w3.org/2002/07/owl#>
    PREFIX {prefix_alias}: <{ontology_uri}>

    SELECT ?action ?scene ?type ?start ?end
    WHERE {{
       ?scene {prefix_alias}:hasAction ?action .
       ?action rdf:type ?type .
       OPTIONAL {{ ?action {prefix_alias}:start_framestamp ?start. }}
       OPTIONAL {{ ?action {prefix_alias}:end_framestamp   ?end. }}
       VALUES ?type {{
         {prefix_alias}:ChangingLane
         {prefix_alias}:PedestrianCrossingLane
         {prefix_alias}:AceleratingHard
         {prefix_alias}:CuttingIn
         {prefix_alias}:CuttingOut
         {prefix_alias}:BrakingHard
         {prefix_alias}:BrakingHardWithPedestrianCrossing
         {prefix_alias}:Following
       }}
       FILTER (?type != owl:Thing)
    }}
    ORDER BY ?scene ?action
    """

    sparql_query_events = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX owl: <http://www.w3.org/2002/07/owl#>
    PREFIX {prefix_alias}: <{ontology_uri}>

    SELECT ?event ?scene ?type ?framestamp
    WHERE {{
       ?scene {prefix_alias}:hasEvent ?event .
       ?event rdf:type ?type .
       OPTIONAL {{ ?event {prefix_alias}:framestamp ?framestamp. }}
       VALUES ?type {{
         {prefix_alias}:PedestrianOnLane
         {prefix_alias}:HardAcceleration
         {prefix_alias}:HardBrake
         {prefix_alias}:HardBrakeWithPedestrianCrossing
         {prefix_alias}:Follows
       }}
       FILTER (?type != owl:Thing)
    }}
    ORDER BY ?scene ?event
    """

    sparql_query_relations = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {prefix_alias}: <{ontology_uri}>

    SELECT ?scene ?relType ?subject ?object
    WHERE {{
      VALUES ?relType {{
        {prefix_alias}:hasObject
        {prefix_alias}:hasObjectFrom
        {prefix_alias}:hasObjectTo
        {prefix_alias}:participatesIn
      }}

      ?subject ?relType ?object .

      {{
        ?scene {prefix_alias}:hasAction ?subject .
      }}
      UNION
      {{
        ?scene {prefix_alias}:hasEvent ?subject .
      }}
      UNION
      {{
        ?scene {prefix_alias}:hasAction ?object .
      }}
      UNION
      {{
        ?scene {prefix_alias}:hasEvent ?object .
      }}
    }}
    """

    sparql_query_hypotenuse = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {prefix_alias}: <{ontology_uri}>

    SELECT ?scene ?vehiculo ?frame ?hypotenuse
    WHERE {{
      ?scene rdf:type {prefix_alias}:scene ;
             {prefix_alias}:hasObject ?vehiculo .
      ?vehiculo {prefix_alias}:hasData ?frame .
      ?frame {prefix_alias}:hypotenuse ?hypotenuse .
    }}
    ORDER BY ?scene ?vehiculo ?frame
    """

    actions_data = {}
    events_data = {}
    relations_data = {}
    hyp_data_by_scene = {}

    res_actions = client.query(sparql_query_actions)
    if res_actions:
        a_proc = ActionsProcessor(res_actions)
        actions_data = a_proc.get_data_by_action()

    res_events = client.query(sparql_query_events)
    if res_events:
        e_proc = EventsProcessor(res_events)
        events_data = e_proc.get_data_by_event()

    res_rel = client.query(sparql_query_relations)
    if res_rel:
        r_proc = RelationshipsProcessor(res_rel)
        relations_data = r_proc.get_data_by_relation()

    res_hyp = client.query(sparql_query_hypotenuse)
    if res_hyp:
        hp_proc = HypotenuseProcessor(res_hyp)
        hyp_data_by_scene = hp_proc.get_data_by_scene()

    inserter = VcdEventsActionsInserter(
        data_by_action=actions_data,
        data_by_event=events_data,
        data_by_relation=relations_data,
        data_by_hypotenuse=hyp_data_by_scene,
        thresholds=thresholds,
        input_dir=input_dir,
        output_dir=output_dir,
    )
    inserter.run()
    print("[OK] Process completed.")


if __name__ == "__main__":
    main()
