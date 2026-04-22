import os
import sys
import yaml
import requests
from typing import Optional, List, Tuple, Dict
from collections import defaultdict
from rdflib import Graph, Namespace, URIRef, Literal
from rdflib.namespace import RDF, XSD

# ------------------------------------------------------
#                  CONFIGURATION                      #
# ------------------------------------------------------
with open('./conf.yaml') as fh:
    read_params = yaml.load(fh, Loader=yaml.FullLoader)

ontology_uri   = read_params["ontology"]["my_uri"]
pref_str       = read_params["ontology"]["pref"]

db_ip          = read_params["database"]["db_ip"]
db_port        = read_params["database"]["db_port"]
db_repo        = read_params["database"]["repository"]

SPARQL_SELECT_ENDPOINT = f"http://{db_ip}:{db_port}/repositories/{db_repo}"

THRESHOLD_ACCELERATION  = read_params['thresholds']['acceleration']
THRESHOLD_BRAKE         = read_params['thresholds']['brake']
THRESHOLD_CUT_IN        = read_params['thresholds']['cut_in_distance_to_ego']
THRESHOLD_CUT_OUT        = read_params['thresholds']['cut_out_distance_to_ego']
THRESHOLD_NEAR_MISS_COLLISION = read_params['thresholds']['near_miss_collision']
THRESHOLD_FOLLOWING    = read_params['thresholds']['following_distance']
MAX_BRAKE_PED_GAP_FRAMES = read_params["thresholds"]["brake_ped_gap_frames"]

# Global RDF graph
graph = Graph()
NS    = Namespace(ontology_uri)
graph.bind(pref_str, ontology_uri)

# ------------------------------------------------------
#                HELPER FUNCTIONS                       #
# ------------------------------------------------------
def get_local_name(uri: str) -> str:
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.rsplit("/", 1)[-1]


def safe_name(s: str) -> str:
    return ''.join(c if c.isalnum() or c in ['-','_'] else '_' for c in s)


def group_consecutive_frames(frames: List[int]) -> List[List[int]]:
    groups: List[List[int]] = []
    if not frames:
        return groups
    current = [frames[0]]
    for f in frames[1:]:
        if f == current[-1] + 1:
            current.append(f)
        else:
            groups.append(current)
            current = [f]
    groups.append(current)
    return groups


def send_select_query(endpoint: str, query: str) -> Optional[dict]:
    headers = {"Accept": "application/sparql-results+json"}
    try:
        resp = requests.get(endpoint, params={"query": query}, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[ERROR] executing SELECT: {e}")
        return None


def serialize_graph(output_file: str) -> None:
    if len(graph):
        output_dir = os.path.dirname(output_file)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        graph.serialize(
            destination=output_file,
            format="nt",
            encoding="utf-8",
        )
        print(f"Tripletas serializadas en {output_file}")
    else:
        print("No se generaron tripletas.")

# ------------------------------------------------------------------
# Cache (scene, object)  →  uid
# ------------------------------------------------------------------
from typing import Dict, Tuple

_uid_cache: Dict[Tuple[str, str], str] = {}

# ------------------------------------------------------------------
#  Global cache   ( scene IRI , object IRI )  →  uid (string)
# ------------------------------------------------------------------
from typing import Dict, Tuple
_uid_cache: Dict[Tuple[str, str], str] = {}

# ─────────────────────────────────────────────────────────────────────────────
#  Global cache  {(scene_iri, obj_iri) → uid_string}
# ─────────────────────────────────────────────────────────────────────────────
_uid_cache: dict[tuple[str, str], str] = {}

# ─────────────────────────────────────────────────────────────────────────────
def get_vcd_uid(scene_iri: str, obj_iri: str) -> str:
    """
    Return the value of :hasUIDinVCD for <obj_iri> inside the <scene_iri> graph.
    If it does not exist, return the object's local-name.
    Results are cached by (scene_iri, obj_iri).
    """
    key = (scene_iri, obj_iri)
    if key in _uid_cache:
        return _uid_cache[key]

    # 1) Get the scene name (scene-0908, scene-0502, …)
    scene_name  = get_local_name(scene_iri)                 # e.g. "scene-0908"

    # 2) Build the named graph IRI
    graph_iri   = f"http://www.openrdf.org/Synergies/{scene_name}"            # e.g. http://www.openrdf.org/nuScenes/scene-0908

    # 3) SPARQL query inside that graph
    sparql = f"""
PREFIX {pref_str}: <{ontology_uri}>
SELECT ?uid
WHERE {{
  GRAPH <{graph_iri}> {{
        <{obj_iri}> {pref_str}:hasUIDinVCD ?uid .
  }}
}}
LIMIT 1
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, sparql)

    # 4) Extract result or use the local-name as a last resort
    if res and res["results"]["bindings"]:
        val = res["results"]["bindings"][0]["uid"]["value"]
    else:
        val = get_local_name(obj_iri)

    # 5) Cache and return
    _uid_cache[key] = val
    return val



# ------------------------------------------------------
#       DETERMINISTIC IRI GENERATION                    #
# ------------------------------------------------------
def make_event_iri(event_type: str, scene_local: str, obj_locals: List[str], frame: int) -> URIRef:
    objs  = "-".join(safe_name(o) for o in obj_locals)
    local = f"{event_type}-{scene_local}-{objs}-frame_{frame}"
    return URIRef(ontology_uri + safe_name(local))


def make_action_iri(action_type: str, scene_local: str, obj_locals: List[str], start: int, end: int) -> URIRef:
    objs  = "-".join(safe_name(o) for o in obj_locals)
    local = f"{action_type}-{scene_local}-{objs}-frames_{start}_{end}"
    return URIRef(ontology_uri + safe_name(local))


def make_frame_iri(scene_iri: str, frame: int) -> URIRef:
    scene_local = safe_name(get_local_name(scene_iri))
    return URIRef(ontology_uri + f"{scene_local}_ego_frame_{frame}")

def make_object_frame_iri(obj_iri: str, frame: int) -> URIRef:
    obj_local = get_local_name(obj_iri)
    return URIRef(ontology_uri + f"{obj_local}_frame_{frame}")

# ------------------------------------------------------
#         TRIPLE INSERTION FUNCTIONS                    #
# ------------------------------------------------------

def add_pedestrian_event(scene: str,  ped: str, pc: str, frame: int) -> None:
    S, P , PC = URIRef(scene), URIRef(ped), URIRef(pc)
    ped_uid = get_vcd_uid(scene, ped)
    pc_uid = get_vcd_uid(scene, pc)
    evt = make_event_iri("PedestrianCrossesZebra", get_local_name(scene), [ped_uid, pc_uid], frame)
    graph.add((S, NS.hasEvent,        evt))
    graph.add((evt, RDF.type,         NS.PedestrianCrossesZebra))
    graph.add((evt, NS.eventID,       Literal(f"{ped_uid}_{pc_uid}_frame{frame}")))
    graph.add((evt, NS.framestamp,    Literal(frame, datatype=XSD.integer)))
    graph.add((evt, NS.hasObject,     P))
    graph.add((evt, NS.hasObject,     PC))
    graph.add((P,   NS.participatesIn,evt))
    graph.add((PC,   NS.participatesIn,evt))

def add_pedestrian_action(scene: str,  ped: str, pc: str, start: int, end: int) -> None:
    S, P , PC = URIRef(scene), URIRef(ped), URIRef(pc)
    ped_uid = get_vcd_uid(scene, ped)
    pc_uid = get_vcd_uid(scene, pc)
    act = make_action_iri("PedestrianCrossingZebra", get_local_name(scene), [ped_uid, pc_uid], start, end)
    graph.add((S,   NS.hasAction,     act))
    graph.add((act, RDF.type,         NS.PedestrianCrossingZebra))
    graph.add((act, NS.actionID,      Literal(f"{ped_uid}_{pc_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObject,     P))
    graph.add((act, NS.hasObject,     PC))
    graph.add((P,   NS.participatesIn,act))
    graph.add((PC,   NS.participatesIn,act))


def add_pedestrian_road_event(scene: str, ped: str, road: str, frame: int) -> None:
    S, P, R = URIRef(scene), URIRef(ped), URIRef(road)
    ped_uid  = get_vcd_uid(scene, ped)
    road_uid = get_vcd_uid(scene, road)
    evt = make_event_iri("PedestrianCrossesRoad", get_local_name(scene),  [ped_uid, road_uid], frame)
    graph.add((S,   NS.hasEvent,        evt))
    graph.add((evt, RDF.type,           NS.PedestrianCrossesRoad))
    graph.add((evt, NS.eventID,         Literal(f"{ped_uid}_{road_uid}_frame{frame}")))
    graph.add((evt, NS.framestamp,      Literal(frame, datatype=XSD.integer)))
    graph.add((evt, NS.hasObject,       P))
    graph.add((evt, NS.hasObject,       R))
    graph.add((P,   NS.participatesIn,  evt))
    graph.add((R,   NS.participatesIn,  evt))


def add_pedestrian_road_action(scene: str, ped: str, road: str, start: int, end: int) -> None:
    S, P, R = URIRef(scene), URIRef(ped), URIRef(road)
    ped_uid  = get_vcd_uid(scene, ped)
    road_uid = get_vcd_uid(scene, road)
    act = make_action_iri("PedestrianCrossingRoad", get_local_name(scene), [ped_uid, road_uid], start, end)
    graph.add((S,   NS.hasAction,       act))
    graph.add((act, RDF.type,           NS.PedestrianCrossingRoad))
    graph.add((act, NS.actionID,        Literal(f"{ped_uid}_{road_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObject,       P))
    graph.add((act, NS.hasObject,       R))
    graph.add((P,   NS.participatesIn,  act))
    graph.add((R,   NS.participatesIn,  act))


def add_hard_acceleration_event(scene: str, veh: str, frame: int) -> None:
    S, V = URIRef(scene), URIRef(veh)
    veh_uid = get_vcd_uid(scene, veh)
    evt  = make_event_iri("HardAcceleration", get_local_name(scene), [veh_uid], frame)
    graph.add((S,   NS.hasEvent,       evt))
    graph.add((evt, RDF.type,          NS.HardAcceleration))
    graph.add((evt, NS.eventID,        Literal(f"{veh_uid}_frame{frame}")))
    graph.add((evt, NS.framestamp,     Literal(frame, datatype=XSD.integer)))
    graph.add((evt, NS.hasObject,      V))
    graph.add((V,   NS.participatesIn, evt))


def add_hard_acceleration_action(scene: str, veh: str, start: int, end: int) -> None:
    S, V = URIRef(scene), URIRef(veh)
    veh_uid = get_vcd_uid(scene, veh)
    act  = make_action_iri("AceleratingHard", get_local_name(scene), [veh_uid], start, end)
    graph.add((S,   NS.hasAction,       act))
    graph.add((act, RDF.type,           NS.AceleratingHard))
    graph.add((act, NS.actionID,        Literal(f"{veh_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObject,       V))
    graph.add((V,   NS.participatesIn,  act))


def add_hypotenuse(frame_iri: URIRef, value: float) -> None:
    graph.add((frame_iri, NS.hypotenuse, Literal(value, datatype=XSD.float)))


def add_hard_brake_event(scene: str, veh: str, frame: int) -> None:
    S, V = URIRef(scene), URIRef(veh)
    veh_uid = get_vcd_uid(scene, veh)
    evt  = make_event_iri("HardBrake", get_local_name(scene), [veh_uid], frame)
    graph.add((S,   NS.hasEvent,       evt))
    graph.add((evt, RDF.type,          NS.HardBrake))
    graph.add((evt, NS.eventID,        Literal(f"{veh_uid}_frame{frame}")))
    graph.add((evt, NS.framestamp,     Literal(frame, datatype=XSD.integer)))
    graph.add((evt, NS.hasObject,      V))
    graph.add((V,   NS.participatesIn, evt))


def add_hard_brake_action(scene: str, veh: str, start: int, end: int) -> None:
    S, V = URIRef(scene), URIRef(veh)
    veh_uid = get_vcd_uid(scene, veh)
    act  = make_action_iri("BrakingHard", get_local_name(scene), [veh_uid], start, end)
    graph.add((S,   NS.hasAction,       act))
    graph.add((act, RDF.type,           NS.BrakingHard))
    graph.add((act, NS.actionID,        Literal(f"{veh_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObject,       V))
    graph.add((V,   NS.participatesIn,  act))


def add_hard_brake_with_ped_event(scene: str, veh: str, ped: str, lane: str, frame: int) -> None:
    S, V, P, L = URIRef(scene), URIRef(veh), URIRef(ped), URIRef(lane)
    veh_uid = get_vcd_uid(scene, veh)
    ped_uid = get_vcd_uid(scene, ped)
    lane_uid = get_vcd_uid(scene, lane)
    evt = make_event_iri(
        "HardBrakeWithPedestrianCrossing",
        get_local_name(scene),
        [veh_uid, ped_uid, lane_uid],
        frame,
    )
    graph.add((S,   NS.hasEvent,       evt))
    graph.add((evt, RDF.type,          NS.HardBrakeWithPedestrianCrossing))
    graph.add((evt, NS.eventID,        Literal(f"{veh_uid}_{ped_uid}_{lane_uid}_frame{frame}")))
    graph.add((evt, NS.framestamp,     Literal(frame, datatype=XSD.integer)))
    graph.add((evt, NS.hasObject,      V))
    graph.add((evt, NS.hasObject,      P))
    graph.add((evt, NS.hasObject,      L))
    graph.add((V,   NS.participatesIn, evt))
    graph.add((P,   NS.participatesIn, evt))
    graph.add((L,   NS.participatesIn, evt))


def add_hard_brake_with_ped_action(scene: str, veh: str, ped: str, lane: str, start: int, end: int) -> None:
    S, V, P, L = URIRef(scene), URIRef(veh), URIRef(ped), URIRef(lane)
    veh_uid = get_vcd_uid(scene, veh)
    ped_uid = get_vcd_uid(scene, ped)
    lane_uid = get_vcd_uid(scene, lane)
    act = make_action_iri(
        "BrakingHardWithPedestrianCrossing",
        get_local_name(scene),
        [veh_uid, ped_uid, lane_uid],
        start,
        end,
    )
    graph.add((S,   NS.hasAction,       act))
    graph.add((act, RDF.type,           NS.BrakingHardWithPedestrianCrossing))
    graph.add((act, NS.actionID,        Literal(f"{veh_uid}_{ped_uid}_{lane_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObject,       V))
    graph.add((act, NS.hasObject,       P))
    graph.add((act, NS.hasObject,       L))
    graph.add((V,   NS.participatesIn,  act))
    graph.add((P,   NS.participatesIn,  act))
    graph.add((L,   NS.participatesIn,  act))


def add_lane_change_action(scene: str, l1: str, l2: str, veh: str, start: int, end: int) -> None:
    S, L1, L2, V = URIRef(scene), URIRef(l1), URIRef(l2), URIRef(veh)
    l1_uid = get_vcd_uid(scene, l1)
    l2_uid = get_vcd_uid(scene, l2)
    v_uid  = get_vcd_uid(scene, veh)
    act = make_action_iri("ChangingLane", get_local_name(scene), [l1_uid, l2_uid, v_uid], start, end)
    graph.add((S,    NS.hasAction,      act))
    graph.add((L1, RDF.type,            NS.LANE))
    graph.add((L2, RDF.type,            NS.LANE))
    graph.add((act, RDF.type,           NS.ChangingLane))
    graph.add((act, NS.actionID,        Literal(f"{l1_uid}_{l2_uid}_{v_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObjectFrom,   L1))
    graph.add((act, NS.hasObjectTo,     L2))
    graph.add((V,   NS.participatesIn,  act))
    graph.add((L1, NS.participatesIn,     act))
    graph.add((L2,   NS.participatesIn,  act))


def add_cut_in_action(scene: str, l1: str, l2: str, veh: str, ego: str, start: int, end: int) -> None:
    S, L1, L2, V, E = URIRef(scene), URIRef(l1), URIRef(l2), URIRef(veh), URIRef(ego)
    l1_uid = get_vcd_uid(scene, l1)
    l2_uid = get_vcd_uid(scene, l2)
    v_uid  = get_vcd_uid(scene, veh)
    e_uid  = get_vcd_uid(scene, ego)
    act = make_action_iri("CuttingIn", get_local_name(scene), [l1_uid, l2_uid, v_uid, e_uid], start, end)
    graph.add((S,   NS.hasAction,       act))
    graph.add((act, RDF.type,           NS.CuttingIn))
    graph.add((act,  NS.actionID,       Literal(f"{l1_uid}_{l2_uid}_{v_uid}_{e_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObjectFrom,   L1))
    graph.add((act, NS.hasObjectTo,     L2))
    graph.add((act, NS.hasObject,   V))
    graph.add((act, NS.hasObject,     E))
    graph.add((V,   NS.participatesIn,  act))
    graph.add((E,   NS.participatesIn,  act))
    graph.add((L1,   NS.participatesIn,  act))
    graph.add((L2,   NS.participatesIn,  act))

def add_cut_out_action(scene: str, veh: str, ego: str, l1: str, l2: str, start: int, end: int) -> None:
    S, V, E, L1, L2 = URIRef(scene), URIRef(veh), URIRef(ego), URIRef(l1), URIRef(l2)
    veh_uid = get_vcd_uid(scene, veh)
    ego_uid = get_vcd_uid(scene, ego)
    l1_uid  = get_vcd_uid(scene, l1)
    l2_uid  = get_vcd_uid(scene, l2)
    act = make_action_iri("CuttingOut", get_local_name(scene), [l1_uid, l2_uid, veh_uid, ego_uid], start, end)
    graph.add((S,    NS.hasAction,      act))
    graph.add((act,  RDF.type,          NS.CuttingOut))
    graph.add((act,  NS.actionID,       Literal(f"{l1_uid}_{l2_uid}_{veh_uid}_{ego_uid}_frames{start}_{end}")))
    graph.add((act,  NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act,  NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act,  NS.hasObjectFrom,  L1))
    graph.add((act,  NS.hasObjectTo,    L2))
    graph.add((act,  NS.hasObject,      V))
    graph.add((act,  NS.hasObject,      E))
    graph.add((V,    NS.participatesIn, act))
    graph.add((E,    NS.participatesIn, act))
    graph.add((L1,   NS.participatesIn, act))
    graph.add((L2,   NS.participatesIn, act))


def add_following_action(scene: str, veh: str, start: int, end: int,  ego_vehicle: str) -> None:
    S, V, EGO = URIRef(scene), URIRef(veh) , URIRef(ego_vehicle)
    veh_uid = get_vcd_uid(scene, veh)
    ego_uid = get_vcd_uid(scene, ego_vehicle)
    # IRI for the following interval (action lasting more than one frame)
    act = make_action_iri("Following", get_local_name(scene), [veh_uid, ego_uid], start, end)
    graph.add((S,   NS.hasAction,       act))
    graph.add((act, RDF.type,           NS.Following))
    graph.add((act, NS.actionID,        Literal(f"{veh_uid}_{ego_uid}_frames_{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObject,       V))
    graph.add((V,   NS.participatesIn,  act))
    graph.add((act, NS.hasObject,       EGO))
    graph.add((EGO,   NS.participatesIn,  act))
    
def add_follows_event(scene: str, veh: str, framestamp: int,  ego_vehicle: str) -> None:
    S, V, EGO = URIRef(scene), URIRef(veh) , URIRef(ego_vehicle)
    veh_uid = get_vcd_uid(scene, veh)
    ego_uid = get_vcd_uid(scene, ego_vehicle)
    # IRI for the following event (single-frame event)
    event = make_event_iri("Follows", get_local_name(scene), [veh_uid, ego_uid], framestamp)
    graph.add((S,   NS.hasEvent,        event))
    graph.add((event, RDF.type,         NS.Follows))
    graph.add((event, NS.eventID,       Literal(f"{veh_uid}_{ego_uid}frame_{framestamp}")))
    graph.add((event, NS.framestamp,    Literal(framestamp, datatype=XSD.integer)))
    graph.add((event, NS.hasObject,     V))
    graph.add((V,   NS.participatesIn,  event))
    graph.add((event, NS.hasObject,       EGO))
    graph.add((EGO,   NS.participatesIn,  event))
    

def add_near_miss_event(scene: str, ego: str, obj: str, frame: int, ttc: float) -> None:
    S, E, O = URIRef(scene), URIRef(ego), URIRef(obj)
    ego_uid = get_vcd_uid(scene, ego)
    obj_uid = get_vcd_uid(scene, obj)
    evt = make_event_iri("NearMissCollision", get_local_name(scene), [obj_uid, ego_uid], frame)
    graph.add((S,      NS.hasEvent,        evt))
    graph.add((evt,    RDF.type,           NS.NearMissCollision))
    graph.add((evt,    NS.eventID,         Literal(f"{obj_uid}_{ego_uid}_frame{frame}")))
    graph.add((evt,    NS.framestamp,      Literal(frame, datatype=XSD.integer)))
    #graph.add((evt,    NS.ttc,             Literal(ttc, datatype=XSD.float)))
    graph.add((evt,    NS.hasObject,       E))
    graph.add((evt,    NS.hasObject,       O))
    graph.add((E,      NS.participatesIn,  evt))
    graph.add((O,      NS.participatesIn,  evt))

def add_near_miss_action(scene: str, ego: str, obj: str,
                         start: int, end: int,
                         ttc_map: Dict[int,float]) -> None:
    S, E, O = URIRef(scene), URIRef(ego), URIRef(obj)
    ego_uid = get_vcd_uid(scene, ego)
    obj_uid = get_vcd_uid(scene, obj)
    act = make_action_iri("NearlyColliding", get_local_name(scene), [obj_uid, ego_uid], start, end)
    graph.add((S,      NS.hasAction,       act))
    graph.add((act,    RDF.type,           NS.NearlyColliding))
    graph.add((act,    NS.actionID,        Literal(f"{obj_uid}_{ego_uid}_frames_{start}_{end}")))
    graph.add((act,    NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act,    NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    # annotate one ttc triple per frame in the range
    #for f, t in ttc_map.items():
        #graph.add((act, NS.ttc, Literal(t, datatype=XSD.float)))
    graph.add((act,    NS.hasObject,       E))
    graph.add((act,    NS.hasObject,       O))
    graph.add((E,      NS.participatesIn,  act))
    graph.add((O,      NS.participatesIn,  act))

def add_pedestrian_lane_event(scene: str, ped: str, lane: str, frame: int) -> None:
    S, P, L = URIRef(scene), URIRef(ped), URIRef(lane)
    ped_uid  = get_vcd_uid(scene, ped)
    lane_uid = get_vcd_uid(scene, lane)
    evt = make_event_iri("PedestrianOnLane", get_local_name(scene), [ped_uid, lane_uid], frame)  
    graph.add((S,   NS.hasEvent,        evt))
    graph.add((evt, RDF.type,           NS.PedestrianOnLane)) 
    graph.add((evt, NS.eventID,         Literal(f"{ped_uid}_{lane_uid}_frame{frame}")))
    graph.add((evt, NS.framestamp,      Literal(frame, datatype=XSD.integer)))
    graph.add((evt, NS.hasObject,       P))
    graph.add((evt, NS.hasObject,       L))
    graph.add((P,   NS.participatesIn,  evt))
    graph.add((L,   NS.participatesIn,  evt))

def add_pedestrian_lane_action(scene: str, ped: str, lane: str, start: int, end: int) -> None:
    S, P, L = URIRef(scene), URIRef(ped), URIRef(lane)
    ped_uid  = get_vcd_uid(scene, ped)
    lane_uid = get_vcd_uid(scene, lane)
    act = make_action_iri("PedestrianCrossingLane", get_local_name(scene), [ped_uid, lane_uid], start, end)
    graph.add((S,   NS.hasAction,       act))
    graph.add((act, RDF.type,           NS.PedestrianCrossingLane))
    graph.add((act, NS.actionID,        Literal(f"{ped_uid}_{lane_uid}_frames{start}_{end}")))
    graph.add((act, NS.start_framestamp,Literal(start, datatype=XSD.integer)))
    graph.add((act, NS.end_framestamp,  Literal(end,   datatype=XSD.integer)))
    graph.add((act, NS.hasObject,       P))
    graph.add((act, NS.hasObject,       L))
    graph.add((P,   NS.participatesIn,  act))
    graph.add((L,   NS.participatesIn,  act))

# ------------------------------------------------------
#  MAIN FUNCTIONS (SELECT + PROCESSING)                 #
# ------------------------------------------------------
def add_scene_filter() -> str:
    return ""  # no scene filter

def handle_normal_vehicle_lane_change():
    filter_str = add_scene_filter()
    select_query = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {pref_str}: <{ontology_uri}>
    
    SELECT ?s ?l1 ?l2 ?f1 ?f2 ?vehicle
    WHERE {{
        ?s rdf:type {pref_str}:scene .
        ?s {pref_str}:hasObject ?vehicle, ?l1, ?l2 .

        ?vehicle rdf:type {pref_str}:VEHICLE .
        ?vehicle {pref_str}:hasData ?od1, ?od2 .

        ?od1 rdf:type {pref_str}:ObjectData ;
             {pref_str}:framestamp ?f1 ;
             {pref_str}:isLocatedIn ?l1 .

        BIND(?f1 + 1 AS ?f2)
             
        ?od2 rdf:type {pref_str}:ObjectData ;
             {pref_str}:framestamp ?f2 ;
             {pref_str}:isLocatedIn ?l2 .
             
        ?l1 rdf:type {pref_str}:LANE .
        ?l2 rdf:type {pref_str}:LANE .
        
        ?l1 {pref_str}:isNextTo ?l2 .
        
        FILTER(?l1 != ?l2)
        
        {filter_str}
    }}
    ORDER BY ?s ?vehicle ?f1 ?f2
    """
    
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        print("No se detectaron cambios de carril de vehículos normales.")
        return

    groups: Dict[Tuple[str,str,str,str], List[Tuple[int,int]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene   = b["s"]["value"]
        l1      = b["l1"]["value"]
        l2      = b["l2"]["value"]
        vehicle = b["vehicle"]["value"]
        try:
            f1 = int(b["f1"]["value"])
            f2 = int(b["f2"]["value"])
        except ValueError:
            continue
        groups[(scene,vehicle,l1,l2)].append((f1,f2))
        
    for (scene,vehicle,l1,l2), pairs in groups.items():
        f1_values = sorted({p[0] for p in pairs})
        for block in group_consecutive_frames(f1_values):
            start = block[0]
            end = block[-1] + 1
            add_lane_change_action(scene, l1, l2, vehicle, start, end)

def handle_pedestrian_on_lane_action_and_event():
    filter_str = add_scene_filter()
    select_query = f"""
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX {pref_str}: <{ontology_uri}>
        
        SELECT ?s ?p ?l ?f
        WHERE {{
            ?p rdf:type {pref_str}:PEDESTRIAN .
            ?l rdf:type {pref_str}:LANE . 
            ?s rdf:type {pref_str}:scene .
            ?od rdf:type {pref_str}:ObjectData .
            ?p {pref_str}:hasData ?od .
            ?s {pref_str}:hasObject ?p .
            ?s {pref_str}:hasObject ?l . 
            ?od {pref_str}:framestamp ?f .
            ?od {pref_str}:isLocatedIn ?l .
            
            {filter_str}
        }}
        ORDER BY ?s ?p ?l ?f
    """
    
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res: return
    group_data: Dict[Tuple[str,str,str], List[int]] = defaultdict(list)
    
    for b in res["results"]["bindings"]:
        scene = b["s"]["value"]
        ped   = b["p"]["value"]
        lane  = b["l"]["value"] 
        f     = int(b["f"]["value"])
        group_data[(scene, ped, lane)].append(f)
        
    for (scene, ped, lane), frames in group_data.items():
        frames.sort()
        for iv in group_consecutive_frames(frames):
            if len(iv) == 1:
                add_pedestrian_lane_event(scene, ped, lane, iv[0])
            else:
                add_pedestrian_lane_action(scene, ped, lane, iv[0], iv[-1])

def handle_pedestrian_crosses_road_action_and_event():
    filter_str = add_scene_filter()
    select_query = f"""
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX {pref_str}: <{ontology_uri}>
        SELECT ?s ?p ?r ?f
        WHERE {{
            ?p rdf:type {pref_str}:PEDESTRIAN .
            ?r rdf:type {pref_str}:ROAD .
            ?s rdf:type {pref_str}:scene .
            ?od rdf:type {pref_str}:ObjectData .
            ?p {pref_str}:hasData ?od .
            ?s {pref_str}:hasObject ?p .
            ?s {pref_str}:hasObject ?r .
            ?od {pref_str}:framestamp ?f .
            ?od {pref_str}:isLocatedIn ?r .
            {filter_str}
        }}
        ORDER BY ?s ?p ?r ?f
    """
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res: return
    group_data: Dict[Tuple[str,str,str], List[int]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene, ped, road, f = b["s"]["value"], b["p"]["value"], b["r"]["value"], int(b["f"]["value"])
        group_data[(scene,ped,road)].append(f)
    for (scene,ped,road), frames in group_data.items():
        frames.sort()
        for iv in group_consecutive_frames(frames):
            if len(iv)==1:
                add_pedestrian_road_event(scene,ped,road, iv[0])
            else:
                add_pedestrian_road_action(scene,ped,road, iv[0], iv[-1])

def handle_hard_acceleration_action_and_event():
    filter_str = add_scene_filter()
    select_query = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {pref_str}: <{ontology_uri}>
    PREFIX f: <http://www.ontotext.com/sparql/functions/>
    SELECT ?s ?vehicle ?f ?hypotenuse
    WHERE {{
      ?s rdf:type {pref_str}:scene .
      ?s {pref_str}:hasObject ?vehicle .
      ?vehicle rdf:type {pref_str}:VEHICLE .
      ?vehicle {pref_str}:hasData ?od .
      ?od rdf:type {pref_str}:ObjectData .
      ?od {pref_str}:framestamp ?f .
      ?od {pref_str}:acceleration_vectorX ?ax .
      ?od {pref_str}:acceleration_vectorY ?ay .
      BIND(f:hypot(?ax, ?ay) AS ?hypotenuse)
      FILTER(f:hypot(?ax, ?ay) >= {THRESHOLD_ACCELERATION})
      FILTER(?ax > 0)
      {filter_str}
    }}
    ORDER BY ?s ?vehicle ?f
    """
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return
    group_data: Dict[Tuple[str,str], List[Tuple[int,float]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["s"]["value"]
        veh   = b["vehicle"]["value"]
        f     = int(b["f"]["value"])
        h     = float(b["hypotenuse"]["value"])
        group_data[(scene,veh)].append((f,h))
    for (scene,veh), records in group_data.items():
        records.sort(key=lambda x: x[0])
        frames = [r[0] for r in records]
        hyp_map = {r[0]: r[1] for r in records}
        for iv in group_consecutive_frames(frames):
            if len(iv) == 1:
                f0 = iv[0]
                add_hard_acceleration_event(scene, veh, f0)
                add_hypotenuse(make_object_frame_iri(veh, f0), hyp_map[f0])
            else:
                start, end = iv[0], iv[-1]
                add_hard_acceleration_action(scene, veh, start, end)
                for f0 in iv:
                    add_hypotenuse(make_object_frame_iri(veh, f0), hyp_map[f0])

def handle_cut_in():
    filter_str = add_scene_filter()
    select_query = f"""
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX ofn:  <http://www.ontotext.com/sparql/functions/>
PREFIX {pref_str}: <{ontology_uri}>

SELECT ?scene ?v ?h ?l1 ?l2 ?f1 ?f2
WHERE {{
  ?scene a {pref_str}:scene ;
         {pref_str}:hasObject ?v, ?h ;
         {pref_str}:hasObject ?l1, ?l2 .

  ?l1 a {pref_str}:LANE .
  ?l2 a {pref_str}:LANE ;
      {pref_str}:isNextTo ?l1 .
  FILTER(?l1 != ?l2)

  ?v a {pref_str}:VEHICLE ;
     {pref_str}:hasData ?od1, ?od2 .
  ?od1 a {pref_str}:ObjectData ;
       {pref_str}:framestamp ?f1 ;
       {pref_str}:isLocatedIn ?l1 .
  
  BIND(?f1 + 1 AS ?f2)

  ?od2 a {pref_str}:ObjectData ;
       {pref_str}:framestamp ?f2 ;
       {pref_str}:isLocatedIn ?l2 ;
       {pref_str}:longitudinalPos ?sv ;
       {pref_str}:bbox3Drotation ?rv .

  ?h a {pref_str}:VEHICLE ;
     {pref_str}:hasData ?odh .
  ?odh a {pref_str}:ObjectData ;
       {pref_str}:framestamp ?f2 ;
       {pref_str}:isLocatedIn ?l2 ;
       {pref_str}:longitudinalPos ?sh ;
       {pref_str}:bbox3Drotation ?rh .

  FILTER(?h != ?v)

  BIND((?sv - ?sh) AS ?de)
  FILTER(?de > 0)
  FILTER(?de < {THRESHOLD_CUT_IN})

  # rotation: normalized angular difference using cos (avoids ±pi jump)
  BIND(ofn:pi() AS ?pi)
  BIND((?rv - ?rh) AS ?drot)
  BIND(ofn:cos(?drot) AS ?cosd)
  FILTER(?cosd > ofn:cos(?pi/4))
  {filter_str}
}}
ORDER BY ?scene ?v ?h ?f2
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    group_data: Dict[Tuple[str,str,str,str,str], List[Tuple[int,int]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["scene"]["value"]
        v     = b["v"]["value"]
        h     = b["h"]["value"]
        l1    = b["l1"]["value"]
        l2    = b["l2"]["value"]
        f1    = int(b["f1"]["value"])
        f2    = int(b["f2"]["value"])
        group_data[(scene, v, h, l1, l2)].append((f1, f2))

    for (scene, v, h, l1, l2), pairs in group_data.items():
        f1_values = sorted({p[0] for p in pairs})
        for block in group_consecutive_frames(f1_values):
            start = block[0]
            end = block[-1] + 1
            add_cut_in_action(scene, l1, l2, v, h, start, end)

def handle_cut_out_action_and_event():
    filter_str = add_scene_filter()
    select_query = f"""
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX ofn:  <http://www.ontotext.com/sparql/functions/>
PREFIX {pref_str}: <{ontology_uri}>

SELECT ?scene ?v ?h ?l1 ?l2 ?f1 ?f2
WHERE {{
  ?scene a {pref_str}:scene ;
         {pref_str}:hasObject ?v, ?h ;
         {pref_str}:hasObject ?l1, ?l2 .

  ?l1 a {pref_str}:LANE .
  ?l2 a {pref_str}:LANE ;
      {pref_str}:isNextTo ?l1 .
  FILTER(?l1 != ?l2)

  ?v a {pref_str}:VEHICLE ;
     {pref_str}:hasData ?od1, ?od2 .
  ?od1 a {pref_str}:ObjectData ;
       {pref_str}:framestamp ?f1 ;
       {pref_str}:isLocatedIn ?l1 ;
       {pref_str}:longitudinalPos ?sv ;
       {pref_str}:bbox3Drotation ?rv .
  
  BIND(?f1 + 1 AS ?f2)

  ?od2 a {pref_str}:ObjectData ;
       {pref_str}:framestamp ?f2 ;
       {pref_str}:isLocatedIn ?l2 .

  ?h a {pref_str}:VEHICLE ;
     {pref_str}:hasData ?odh1, ?odh2 .
  ?odh1 a {pref_str}:ObjectData ;
        {pref_str}:framestamp ?f1 ;
        {pref_str}:isLocatedIn ?l1 ;
        {pref_str}:longitudinalPos ?sh ;
        {pref_str}:bbox3Drotation ?rh .
  ?odh2 a {pref_str}:ObjectData ;
        {pref_str}:framestamp ?f2 ;
        {pref_str}:isLocatedIn ?l1 .

  FILTER(?h != ?v)

  BIND((?sv - ?sh) AS ?de)
  FILTER(?de > 0)
  FILTER(?de < {THRESHOLD_CUT_OUT})

  # rotation: normalized angular difference using cos (avoids ±pi jump)
  BIND(ofn:pi() AS ?pi)
  BIND((?rv - ?rh) AS ?drot)
  BIND(ofn:cos(?drot) AS ?cosd)
  FILTER(?cosd > ofn:cos(?pi/4))
  {filter_str}
}}
ORDER BY ?scene ?v ?h ?f2
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    group_data: Dict[Tuple[str,str,str,str,str], List[Tuple[int,int]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["scene"]["value"]
        v     = b["v"]["value"]
        h     = b["h"]["value"]
        l1    = b["l1"]["value"]
        l2    = b["l2"]["value"]
        f1    = int(b["f1"]["value"])
        f2    = int(b["f2"]["value"])
        group_data[(scene, v, h, l1, l2)].append((f1, f2))

    for (scene, v, h, l1, l2), pairs in group_data.items():
        f1_values = sorted({p[0] for p in pairs})
        for block in group_consecutive_frames(f1_values):
            start = block[0]
            end = block[-1] + 1
            add_cut_out_action(scene, v, h, l1, l2, start, end)




####################################################### DIFFERENT FROM NUSCENES ########################
def handle_pedestrian_crosses_zebra_action_and_event():
    select_query = f"""
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX {pref_str}: <{ontology_uri}>

SELECT DISTINCT ?s ?p ?pc ?f
WHERE {{

    ?p rdf:type {pref_str}:PEDESTRIAN .
    ?pc rdf:type {pref_str}:ped_crossing .


    ?s rdf:type {pref_str}:scene ;
       {pref_str}:hasObject  ?p ;
       {pref_str}:hasObject  ?pc .


    ?od rdf:type {pref_str}:ObjectData ;
        {pref_str}:framestamp   ?f ;
        {pref_str}:isLocatedIn  ?pc .
    ?p {pref_str}:hasData     ?od .
}}
ORDER BY ?s ?p ?f
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    # group by (scene, pedestrian) all timestamps
    group_data: Dict[Tuple[str,str,str], List[int]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene_iri = b["s"]["value"]
        ped_iri   = b["p"]["value"]
        pc_iri    = b["pc"]["value"]
        f         = int(b["f"]["value"])
        group_data[(scene_iri, ped_iri, pc_iri)].append(f)

    # for each (scene, pedestrian) generate an event or action
    for (scene_iri, ped_iri, pc_iri), frames in group_data.items():
        unique_frames = sorted(set(frames))
        # single frame => event
        if len(unique_frames) == 1:
            add_pedestrian_event(scene_iri, ped_iri, pc_iri, unique_frames[0])
        else:
            # multiple frames => action for each consecutive block
            for iv in group_consecutive_frames(unique_frames):
                if len(iv) == 1:
                    add_pedestrian_event(scene_iri, ped_iri, pc_iri ,  iv[0])
                else:
                    add_pedestrian_action(scene_iri, ped_iri, pc_iri, iv[0], iv[-1])


##################################################### EGO ##############################################
def handle_hard_acceleration_action_and_event_ego():
    filter_str = add_scene_filter()
    select_query = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {pref_str}: <{ontology_uri}>
    PREFIX f: <http://www.ontotext.com/sparql/functions/>
    SELECT ?s ?vehicle ?f ?hypotenuse
    WHERE {{
      ?s rdf:type {pref_str}:scene .
      ?ed rdf:type {pref_str}:EgoData .
      ?s {pref_str}:hasEgoData ?ed .
      ?ed {pref_str}:framestamp ?f .
      ?ed {pref_str}:acceleration_vectorX ?ax .
      ?ed {pref_str}:acceleration_vectorY ?ay .
      ?vehicle {pref_str}:hasData ?ed .
      BIND(f:hypot(?ax, ?ay) AS ?hypotenuse)
      FILTER(f:hypot(?ax, ?ay) >= {THRESHOLD_ACCELERATION})
      FILTER(?ax > 0)
      {filter_str}
    }}
    ORDER BY ?s ?vehicle ?f
    """
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return
    group_data: Dict[Tuple[str,str], List[Tuple[int,float]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["s"]["value"]
        veh   = b["vehicle"]["value"]
        f     = int(b["f"]["value"])
        h     = float(b["hypotenuse"]["value"])
        group_data[(scene,veh)].append((f,h))
    for (scene,veh), records in group_data.items():
        records.sort(key=lambda x: x[0])
        frames = [r[0] for r in records]
        hyp_map = {r[0]: r[1] for r in records}
        for iv in group_consecutive_frames(frames):
            if len(iv) == 1:
                f0 = iv[0]
                add_hard_acceleration_event(scene, veh, f0)
                add_hypotenuse(make_frame_iri(scene, f0), hyp_map[f0])
            else:
                start, end = iv[0], iv[-1]
                add_hard_acceleration_action(scene, veh, start, end)
                for f0 in iv:
                    add_hypotenuse(make_frame_iri(scene, f0), hyp_map[f0])

def handle_cut_in_ego():
    select_query = f"""
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX ofn:  <http://www.ontotext.com/sparql/functions/>
PREFIX {pref_str}: <{ontology_uri}>

SELECT
  ?scene
  ?v
  ?ego
  ?l1
  ?l2
  (MIN(?fs1) AS ?start)
  (MAX(?fs2) AS ?end)
WHERE {{
  # Scene, its EgoData and the ego_vehicle
  ?scene a {pref_str}:scene ;
         {pref_str}:hasEgoData  ?ed ;
         {pref_str}:hasObject   ?l1, ?l2, ?v .
  ?ed    a {pref_str}:EgoData ;
         {pref_str}:framestamp   ?fs2 ;
         {pref_str}:ego_rotation  ?r2 ;
         {pref_str}:isLocatedIn  ?l2 .
  ?ego   {pref_str}:hasData     ?ed .

  # Lanes
  ?l1 a {pref_str}:LANE .
  ?l2 a {pref_str}:LANE ;
      {pref_str}:isNextTo       ?l1 .
  FILTER(?l1 != ?l2)

  # Cut-in candidate vehicle
  ?v a {pref_str}:VEHICLE ;
     {pref_str}:hasData       ?od1, ?od2 .
  ?od1 a {pref_str}:ObjectData ;
       {pref_str}:framestamp   ?fs1 ;
       {pref_str}:isLocatedIn  ?l1 .
  ?od2 a {pref_str}:ObjectData ;
       {pref_str}:framestamp      ?fs2 ;
       {pref_str}:isLocatedIn     ?l2 ;
       {pref_str}:distance_to_ego ?de ;
       {pref_str}:front_of_ego    ?front ;
       {pref_str}:bbox3Drotation  ?r1 .

  FILTER(str(?front) = "true")
  FILTER(?de   < {THRESHOLD_CUT_IN})
  FILTER(?fs2 > ?fs1)

  BIND(ofn:pi() AS ?pi)
  FILTER(
    ?r1 > (?r2 - (?pi/4)) &&
    ?r1 < (?r2 + (?pi/4))
  )
}}
GROUP BY ?scene ?v ?ego ?l1 ?l2
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    # Each result already includes the min–max interval
    for b in res["results"]["bindings"]:
        scene = b["scene"]["value"]
        v     = b["v"]["value"]
        ego   = b["ego"]["value"]
        l1    = b["l1"]["value"]
        l2    = b["l2"]["value"]
        start = int(b["start"]["value"])
        end   = int(b["end"]["value"])

        add_cut_in_action(scene, l1, l2, v, ego, start, end)

def handle_following_action_and_event_ego():
    """
    Detects 'Follows' (event) and 'Following' (action):
    - For each scene and egoVehicle
    - For each frame: select the vehicle with the smallest distance_to_ego
    - Group consecutive frames while it is the SAME vehicle
    """
    select_query = f"""
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
PREFIX {pref_str}: <{ontology_uri}>

SELECT DISTINCT ?scene ?egoVehicle ?veh ?frame ?d
WHERE {{
  # ego-frame and its lane
  ?scene  rdf:type            {pref_str}:scene ;
          {pref_str}:hasEgoData ?egoFrame ;
          {pref_str}:hasObject ?veh .

  ?veh rdf:type          {pref_str}:VEHICLE .
  ?veh        {pref_str}:hasData   ?vehFrame .

  ?lane rdf:type          {pref_str}:LANE .
  ?egoFrame rdf:type          {pref_str}:EgoData ;
            {pref_str}:framestamp ?frame ;
            {pref_str}:isLocatedIn ?lane .
  ?egoVehicle {pref_str}:hasData ?egoFrame .

  # candidate ahead of the ego in the SAME lane
  ?vehFrame rdf:type          {pref_str}:ObjectData ;
            {pref_str}:framestamp ?frame ;
            {pref_str}:isLocatedIn ?lane ;
            {pref_str}:front_of_ego ?front ;
            {pref_str}:distance_to_ego ?d ;
            {pref_str}:attr            ?attrVal .

  FILTER(str(?front) = "true")
  FILTER( str(?attrVal) != "vehicle.parked" )
  FILTER(?d < {THRESHOLD_FOLLOWING})
  ?veh {pref_str}:hasData ?vehFrame .
}}
ORDER BY ?scene ?egoVehicle ?frame
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    # --------------------------------------------------------------------------------
    # 1)  group by (scene, egoVehicle, frame) all pairs (veh, distance)
    # --------------------------------------------------------------------------------
    from collections import defaultdict
    by_scene_ego = defaultdict(lambda: defaultdict(list))  # {(scene,ego): {frame:[(veh,d)]}}
    for b in res["results"]["bindings"]:
        key  = (b["scene"]["value"], b["egoVehicle"]["value"])
        fra  = int(float(b["frame"]["value"]))             # GraphDB sometimes returns it as 4.0
        veh  = b["veh"]["value"]
        dist = float(b["d"]["value"])
        # keep only the minimum distance found for that car in that frame
        frames = by_scene_ego[key][fra]
        if veh in dict(frames):                       # we already had it
            prev_min = min(d for v, d in frames if v == veh)
            if dist < prev_min:                       # keep the smaller one
                frames[:] = [(v, d) for v, d in frames if v != veh] + [(veh, dist)]
        else:
            frames.append((veh, dist))


    # --------------------------------------------------------------------------------
    # 2)  iterate through ordered frames: choose the vehicle with the minimum distance,
    #     build sequences while it is the same vehicle
    # --------------------------------------------------------------------------------
    for (scene, ego), frame_map in by_scene_ego.items():
        frames_sorted = sorted(frame_map.keys())
        current_veh   = None
        seq_start     = None
        prev_frame    = None

        def _close_sequence(v, start_f, end_f):
            if v is None:  # nothing open yet
                return
            if start_f == end_f:
                add_follows_event(scene, v, start_f, ego)
            else:
                add_following_action(scene, v, start_f, end_f, ego)

        for f in frames_sorted:
            # closest vehicle in this frame
            nearest_veh = min(frame_map[f], key=lambda t: t[1])[0]

            if nearest_veh != current_veh:      # --------- breaks the sequence
                _close_sequence(current_veh, seq_start, prev_frame)
                current_veh = nearest_veh
                seq_start   = f
            # if it's the same vehicle, just continue
            prev_frame = f

        # close the last sequence
        _close_sequence(current_veh, seq_start, prev_frame)

def handle_following_action_and_event():
    """
    Detects 'Follows' (event) and 'Following' (action) between vehicles:
    - For each scene and follower vehicle
    - For each frame: select the closest vehicle ahead in the SAME lane
    - Ignore stopped candidates (speed < 0.5)
    - Group consecutive frames while it is the SAME target vehicle
    """
    select_query = f"""
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX {pref_str}: <{ontology_uri}>
PREFIX f: <http://www.ontotext.com/sparql/functions/>

SELECT DISTINCT ?scene ?follower ?target ?frame ?d
WHERE {{
  ?scene rdf:type {pref_str}:scene ;
         {pref_str}:hasObject ?follower, ?target, ?lane .

  ?follower rdf:type {pref_str}:VEHICLE ;
            {pref_str}:hasData ?fFrame .
  ?target rdf:type {pref_str}:VEHICLE ;
          {pref_str}:hasData ?tFrame .

  ?lane rdf:type {pref_str}:LANE .

  ?fFrame rdf:type {pref_str}:ObjectData ;
          {pref_str}:framestamp ?frame ;
          {pref_str}:isLocatedIn ?lane ;
          {pref_str}:longitudinalPos ?sv .

  ?tFrame rdf:type {pref_str}:ObjectData ;
          {pref_str}:framestamp ?frame ;
          {pref_str}:isLocatedIn ?lane ;
          {pref_str}:longitudinalPos ?sh ;
          {pref_str}:velocity_vectorX ?vx ;
          {pref_str}:velocity_vectorY ?vy .

  FILTER(?target != ?follower)
  BIND((?sh - ?sv) AS ?d)
  FILTER(?d > 0)
  FILTER(?d < {THRESHOLD_FOLLOWING})

  BIND(f:hypot(?vx, ?vy) AS ?speed)
  FILTER(?speed >= 0.5)
}}
ORDER BY ?scene ?follower ?frame
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    # --------------------------------------------------------------------------------
    # 1) group by (scene, follower, frame) all pairs (target, distance)
    # --------------------------------------------------------------------------------
    from collections import defaultdict
    by_scene_follower = defaultdict(lambda: defaultdict(list))  # {(scene,follower): {frame:[(target,d)]}}
    for b in res["results"]["bindings"]:
        key  = (b["scene"]["value"], b["follower"]["value"])
        fra  = int(float(b["frame"]["value"]))             # GraphDB sometimes returns it as 4.0
        tgt  = b["target"]["value"]
        dist = float(b["d"]["value"])
        frames = by_scene_follower[key][fra]
        if tgt in dict(frames):
            prev_min = min(d for v, d in frames if v == tgt)
            if dist < prev_min:
                frames[:] = [(v, d) for v, d in frames if v != tgt] + [(tgt, dist)]
        else:
            frames.append((tgt, dist))

    # --------------------------------------------------------------------------------
    # 2) iterate frames: pick the closest target, group sequences
    # --------------------------------------------------------------------------------
    for (scene, follower), frame_map in by_scene_follower.items():
        frames_sorted = sorted(frame_map.keys())
        current_target = None
        seq_start = None
        prev_frame = None

        def _close_sequence(target, start_f, end_f):
            if target is None:
                return
            if start_f == end_f:
                add_follows_event(scene, target, start_f, follower)
            else:
                add_following_action(scene, target, start_f, end_f, follower)

        for f in frames_sorted:
            nearest_target = min(frame_map[f], key=lambda t: t[1])[0]
            if nearest_target != current_target:
                _close_sequence(current_target, seq_start, prev_frame)
                current_target = nearest_target
                seq_start = f
            prev_frame = f

        _close_sequence(current_target, seq_start, prev_frame)

def handle_near_miss_action_and_event_ego():
    """
    Detects all ObjectData with TTC ≤ 1.5 s (in the same scene, without requiring the same frame),
    groups by (scene, egoVehicle, object) and creates events or actions.
    """
    select_query = f"""
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
PREFIX {pref_str}: <{ontology_uri}>

SELECT DISTINCT ?scene ?egoVehicle ?obj ?frame ?ttc
WHERE {{
  ?scene  rdf:type   {pref_str}:scene ;
          {pref_str}:hasEgoData   ?egoFrame ;
          {pref_str}:hasObject    ?obj .
  ?egoVehicle {pref_str}:hasData ?egoFrame .
  ?obj        {pref_str}:hasData ?objFrame .
  ?objFrame  {pref_str}:TTC       ?ttc ;
             {pref_str}:framestamp ?frame .
  FILTER( ?ttc <= {THRESHOLD_NEAR_MISS_COLLISION} )
}}
ORDER BY ?scene ?egoVehicle ?obj ?frame
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    # accumulate all (frame, ttc) by (scene, ego, obj)
    from collections import defaultdict
    group_data: Dict[Tuple[str,str,str], List[Tuple[int,float]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["scene"]["value"]
        ego   = b["egoVehicle"]["value"]
        obj   = b["obj"]["value"]
        frame = int(b["frame"]["value"])
        ttc   = float(b["ttc"]["value"])
        group_data[(scene,ego,obj)].append((frame, ttc))

    # for each triple, create an event (1 frame) or an action (>1 frame)
    for (scene, ego, obj), records in group_data.items():
        # sort and separate unique frames
        records = sorted(set(records), key=lambda x: x[0])
        frames = [r[0] for r in records]
        ttc_map = {r[0]: r[1] for r in records}
        for block in group_consecutive_frames(frames):
            if len(block) == 1:
                f0 = block[0]
                add_near_miss_event(scene, ego, obj, f0, ttc_map[f0])
            else:
                start, end = block[0], block[-1]
                # pass only the ttc values for that block
                submap = {f: ttc_map[f] for f in block}
                add_near_miss_action(scene, ego, obj, start, end, submap)

def handle_cut_out_action_and_event_ego():
    select_query = f"""
PREFIX ofn:  <http://www.ontotext.com/sparql/functions/>
PREFIX {pref_str}: <{ontology_uri}>

SELECT
  ?scene
  ?v
  ?ego
  ?l1
  ?l2
  (MIN(?fs1) AS ?start)
  (MAX(?fs2) AS ?end)
WHERE {{
  ?scene a {pref_str}:scene ;
         {pref_str}:hasEgoData   ?ed ;
         {pref_str}:hasObject    ?l1, ?l2, ?v .
  ?ed    a {pref_str}:EgoData ;
         {pref_str}:framestamp  ?fs1 ;
         {pref_str}:ego_rotation ?r2 ;
         {pref_str}:isLocatedIn ?l1 .
  ?ego   {pref_str}:hasData    ?ed .

  ?l1 a {pref_str}:LANE .
  ?l2 a {pref_str}:LANE ;
      {pref_str}:isNextTo      ?l1 .
  FILTER(?l1 != ?l2)

  ?v   a {pref_str}:VEHICLE ;
       {pref_str}:hasData      ?od1, ?od2 .

  ?od1 a {pref_str}:ObjectData ;
       {pref_str}:framestamp     ?fs1 ;
       {pref_str}:isLocatedIn    ?l1 ;
       {pref_str}:front_of_ego   ?front ;
       {pref_str}:distance_to_ego?de ;
       {pref_str}:bbox3Drotation ?r1 .
  FILTER(str(?front)="true")
  FILTER(?de < {THRESHOLD_CUT_OUT})

  ?od2 a {pref_str}:ObjectData ;
       {pref_str}:framestamp    ?fs2 ;
       {pref_str}:isLocatedIn   ?l2 .
  FILTER(?fs2 > ?fs1)

  BIND(ofn:pi() AS ?pi)
  FILTER(
    ?r1 > (?r2 - (?pi/4)) &&
    ?r1 < (?r2 + (?pi/4))
  )
}}
GROUP BY ?scene ?v ?ego ?l1 ?l2
ORDER BY ?scene ?v ?start
"""
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    for b in res["results"]["bindings"]:
        scene = b["scene"]["value"]
        v     = b["v"]["value"]
        ego   = b["ego"]["value"]
        l1    = b["l1"]["value"]
        l2    = b["l2"]["value"]
        start = int(b["start"]["value"])
        end   = int(b["end"]["value"])

        add_cut_out_action(scene, v, ego, l1, l2, start, end)

def handle_hard_brake_action_and_event_ego():
    filter_str = add_scene_filter()
    select_query = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {pref_str}: <{ontology_uri}>
    PREFIX f: <http://www.ontotext.com/sparql/functions/>
    SELECT ?s ?vehicle ?f ?hypotenuse
    WHERE {{
      ?s rdf:type {pref_str}:scene .
      ?ed rdf:type {pref_str}:EgoData .
      ?s {pref_str}:hasEgoData ?ed .
      ?ed {pref_str}:framestamp ?f .
      ?ed {pref_str}:acceleration_vectorX ?ax .
      ?ed {pref_str}:acceleration_vectorY ?ay .
      ?vehicle {pref_str}:hasData ?ed .
      BIND(f:hypot(?ax, ?ay) AS ?hypotenuse)
      FILTER(f:hypot(?ax, ?ay) >= {THRESHOLD_BRAKE})
      FILTER(?ax < 0)
      {filter_str}
    }}
    ORDER BY ?s ?vehicle ?f
    """
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return
    group_data: Dict[Tuple[str,str], List[Tuple[int,float]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["s"]["value"]
        veh   = b["vehicle"]["value"]
        f     = int(b["f"]["value"])
        h     = float(b["hypotenuse"]["value"])
        group_data[(scene,veh)].append((f,h))
    for (scene,veh), records in group_data.items():
        records.sort(key=lambda x: x[0])
        frames = [r[0] for r in records]
        hyp_map = {r[0]: r[1] for r in records}
        for iv in group_consecutive_frames(frames):
            if len(iv) == 1:
                f0 = iv[0]
                add_hard_brake_event(scene, veh, f0)
                add_hypotenuse(make_frame_iri(scene, f0), hyp_map[f0])
            else:
                start, end = iv[0], iv[-1]
                add_hard_brake_action(scene, veh, start, end)
                for f0 in iv:
                    add_hypotenuse(make_frame_iri(scene, f0), hyp_map[f0])

def handle_hard_brake_action_and_event():
    filter_str = add_scene_filter()
    select_query = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {pref_str}: <{ontology_uri}>
    PREFIX f: <http://www.ontotext.com/sparql/functions/>
    SELECT ?s ?vehicle ?f ?hypotenuse
    WHERE {{
      ?s rdf:type {pref_str}:scene .
      ?s {pref_str}:hasObject ?vehicle .
      ?vehicle rdf:type {pref_str}:VEHICLE .
      ?vehicle {pref_str}:hasData ?od .
      ?od rdf:type {pref_str}:ObjectData .
      ?od {pref_str}:framestamp ?f .
      ?od {pref_str}:acceleration_vectorX ?ax .
      ?od {pref_str}:acceleration_vectorY ?ay .
      BIND(f:hypot(?ax, ?ay) AS ?hypotenuse)
      FILTER(f:hypot(?ax, ?ay) >= {THRESHOLD_BRAKE})
      FILTER(?ax < 0)
      {filter_str}
    }}
    ORDER BY ?s ?vehicle ?f
    """
    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return
    group_data: Dict[Tuple[str,str], List[Tuple[int,float]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["s"]["value"]
        veh   = b["vehicle"]["value"]
        f     = int(b["f"]["value"])
        h     = float(b["hypotenuse"]["value"])
        group_data[(scene,veh)].append((f,h))
    for (scene,veh), records in group_data.items():
        records.sort(key=lambda x: x[0])
        frames = [r[0] for r in records]
        hyp_map = {r[0]: r[1] for r in records}
        for iv in group_consecutive_frames(frames):
            if len(iv) == 1:
                f0 = iv[0]
                add_hard_brake_event(scene, veh, f0)
                add_hypotenuse(make_object_frame_iri(veh, f0), hyp_map[f0])
            else:
                start, end = iv[0], iv[-1]
                add_hard_brake_action(scene, veh, start, end)
                for f0 in iv:
                    add_hypotenuse(make_object_frame_iri(veh, f0), hyp_map[f0])


def handle_hard_brake_with_pedestrian_crossing():
    """
    Detects hard brake + pedestrian on lane combinations.
    Cases:
      - Partial or full overlap: interval = [min(start), max(end)].
      - No overlap, brake first and gap <= MAX_BRAKE_PED_GAP_FRAMES:
        interval = [bStart, pEnd].
    Selects the closest pedestrian by temporal gap (0 if they overlap).
    Note: requires base events/actions already in GraphDB.
    """
    filter_str = add_scene_filter()
    select_query = f"""
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX {pref_str}: <{ontology_uri}>
    SELECT ?scene ?veh ?ped ?lane ?veh_lane ?bStart ?bEnd ?pStart ?pEnd
    WHERE {{
      ?scene rdf:type {pref_str}:scene .
      {{
        ?scene {pref_str}:hasEvent ?b .
        ?b rdf:type {pref_str}:HardBrake ;
           {pref_str}:framestamp ?bStart ;
           {pref_str}:hasObject ?veh .
        BIND(?bStart AS ?bEnd)
      }} UNION {{
        ?scene {pref_str}:hasAction ?b .
        ?b rdf:type {pref_str}:BrakingHard ;
           {pref_str}:start_framestamp ?bStart ;
           {pref_str}:end_framestamp ?bEnd ;
           {pref_str}:hasObject ?veh .
      }}

      {{
        ?scene {pref_str}:hasEvent ?p .
        ?p rdf:type {pref_str}:PedestrianOnLane ;
           {pref_str}:framestamp ?pStart ;
           {pref_str}:hasObject ?ped ;
           {pref_str}:hasObject ?lane .
        BIND(?pStart AS ?pEnd)
      }} UNION {{
        ?scene {pref_str}:hasAction ?p .
        ?p rdf:type {pref_str}:PedestrianCrossingLane ;
           {pref_str}:start_framestamp ?pStart ;
           {pref_str}:end_framestamp ?pEnd ;
           {pref_str}:hasObject ?ped ;
           {pref_str}:hasObject ?lane .
      }}

      # Reference frame: first frame where both have already started
      # ref = max(bStart, pStart)
      BIND(IF(?bStart > ?pStart, ?bStart, ?pStart) AS ?ref)

      # Vehicle and pedestrian in the same lane in the ref frame
      ?veh {pref_str}:hasData ?od .
      ?od rdf:type {pref_str}:ObjectData ;
          {pref_str}:framestamp ?ref ;
          {pref_str}:isLocatedIn ?veh_lane ;
          {pref_str}:longitudinalPos ?veh_long .

      ?ped {pref_str}:hasData ?pod .
      ?pod rdf:type {pref_str}:ObjectData ;
           {pref_str}:framestamp ?ref ;
           {pref_str}:isLocatedIn ?lane ;
           {pref_str}:longitudinalPos ?ped_long .

      # Require the same lane
      FILTER(?veh_lane = ?lane)

      # Vehicle direction (derived from longitudinalPos) to decide "pedestrian ahead"
      # 1) Change between bStart and bEnd (always recorded)
      ?veh {pref_str}:hasData ?od_bstart .
      ?od_bstart rdf:type {pref_str}:ObjectData ;
                 {pref_str}:framestamp ?bStart ;
                 {pref_str}:longitudinalPos ?veh_long_start .
      ?veh {pref_str}:hasData ?od_bend .
      ?od_bend rdf:type {pref_str}:ObjectData ;
               {pref_str}:framestamp ?bEnd ;
               {pref_str}:longitudinalPos ?veh_long_end .

      # 2) Plan B: if the car hasn't moved and we don't know the direction from the event
      BIND((?ref - 1) AS ?ref_prev)
      BIND((?ref + 1) AS ?ref_next)
      OPTIONAL {{
        ?veh {pref_str}:hasData ?od_prev .
        ?od_prev rdf:type {pref_str}:ObjectData ;
                {pref_str}:framestamp ?ref_prev ;
                {pref_str}:longitudinalPos ?veh_long_prev .
      }}
      OPTIONAL {{
        ?veh {pref_str}:hasData ?od_next .
        ?od_next rdf:type {pref_str}:ObjectData ;
                {pref_str}:framestamp ?ref_next ;
                {pref_str}:longitudinalPos ?veh_long_next .
      }}
      # Local longitudinalPos change around ref (ref-1 or ref+1)
      BIND(
        IF(BOUND(?veh_long_prev), (?veh_long - ?veh_long_prev),
          IF(BOUND(?veh_long_next), (?veh_long_next - ?veh_long), 0)
        ) AS ?dlong_local
      )
      # Main vehicle change: bStart/bEnd if valid, otherwise local change
      BIND(
        IF((?veh_long_end != ?veh_long_start),
           (?veh_long_end - ?veh_long_start),
           ?dlong_local
        ) AS ?dlong
      )
      # Longitudinal difference at ref (pedestrian - vehicle)
      BIND((?ped_long - ?veh_long) AS ?front_long)
      # If the vehicle moves with increasing longitude, ped_long >= veh_long
      # If it moves with decreasing longitude, ped_long <= veh_long
      FILTER(
        (?dlong > 0 && ?front_long >= 0) ||
        (?dlong < 0 && ?front_long <= 0) ||
        (?dlong = 0)
      )

      # Temporal overlap or allowed gap (brake before)
      # gap = pStart - bEnd - 1
      FILTER(
        ( ?pStart <= ?bEnd && ?pEnd >= ?bStart ) ||
        ( ?bEnd < ?pStart && (?pStart - ?bEnd - 1) <= {MAX_BRAKE_PED_GAP_FRAMES} )
      )
      {filter_str}
    }}
    ORDER BY ?scene ?veh ?bStart ?pStart
    """

    res = send_select_query(SPARQL_SELECT_ENDPOINT, select_query)
    if not res:
        return

    grouped: Dict[Tuple[str, str, int, int], List[Tuple[int, int, str, str, str]]] = defaultdict(list)
    for b in res["results"]["bindings"]:
        scene = b["scene"]["value"]
        veh = b["veh"]["value"]
        ped = b["ped"]["value"]
        lane = b["lane"]["value"]
        veh_lane = b["veh_lane"]["value"]
        b_start = int(float(b["bStart"]["value"]))
        b_end = int(float(b["bEnd"]["value"]))
        p_start = int(float(b["pStart"]["value"]))
        p_end = int(float(b["pEnd"]["value"]))
        key = (scene, veh, b_start, b_end)
        grouped[key].append((p_start, p_end, ped, lane, veh_lane))

    for (scene, veh, b_start, b_end), ped_list in grouped.items():
        # Candidates for "associated pedestrian" for this hard brake
        # Each candidate stores an ordering key and the final interval
        candidates: List[Tuple[int, int, int, str, str, int, int]] = []

        for p_start, p_end, ped, lane, veh_lane in ped_list:
            # Is there temporal overlap between hard brake and crossing?
            overlap = (p_start <= b_end and p_end >= b_start)
            if overlap:
                # Overlap case: the final interval is the union
                start = min(b_start, p_start)
                end = max(b_end, p_end)
                gap = 0
            else:
                # No-overlap case: accept only if the brake happens before
                # and the gap between them is small
                gap = p_start - b_end - 1
                if b_end < p_start and gap <= MAX_BRAKE_PED_GAP_FRAMES:
                    start = b_start
                    end = p_end
                else:
                    continue
            # Ordering key:
            # 1) smaller gap (0 if they overlap)
            # 2) p_start closer to b_start
            # 3) smaller p_start
            candidates.append((gap, abs(p_start - b_start), p_start, ped, lane, start, end))

        if not candidates:
            continue

        # Choose the "closest" candidate according to the ordering key
        gap, _, p_start, ped, lane, start, end = min(
            candidates, key=lambda t: (t[0], t[1], t[2], t[3])
        )

        if start == end:
            add_hard_brake_with_ped_event(scene, veh, ped, lane, start)
        else:
            add_hard_brake_with_ped_action(scene, veh, ped, lane, start, end)


def execute():
    # Base queries (needed before queries_extras.py)
    handle_pedestrian_on_lane_action_and_event()
    handle_hard_brake_action_and_event()

    #handle_normal_vehicle_lane_change()
    #handle_hard_acceleration_action_and_event()
    #handle_cut_in()
    #handle_cut_out_action_and_event()
    #handle_following_action_and_event()


##############################################################################
    #NO CROSSWALK AVAILABLE
    #handle_pedestrian_crosses_zebra_action_and_event()

    #NO ROAD: isLocatedIn only points to lane
    #handle_pedestrian_crosses_road_action_and_event()

    #NO TTC
    #handle_near_miss_action_and_event()

##############################################################################
    #FUTURE EGO
    #handle_hard_acceleration_action_and_event_ego()
    #handle_cut_in_ego()
    #handle_cut_out_action_and_event_ego() 
    #handle_following_action_and_event_ego()
    #handle_near_miss_action_and_event_ego()
    #handle_hard_brake_action_and_event_ego()
    
    output_file = read_params.get("outputs", {}).get("queries_file")
    if not output_file:
        print("[ERROR] Missing outputs.queries_file in conf.yaml")
        return
    serialize_graph(output_file)

if __name__ == "__main__":
    execute()
