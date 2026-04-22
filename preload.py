"""
Pipeline: parse VCD files, map objects/frames to RDF, and serialize each scene
to N-Quads. Uses multiprocessing to create per-scene .nq chunks and merges them
into a single output file.
"""

import argparse
import os
import vcd.core as core
import vcd.types as types
from vcd.core import VCD, ElementType, FrameIntervals
from datetime import datetime
from SPARQLWrapper import SPARQLWrapper, POST, DIGEST, JSON
from SPARQLBurger.SPARQLQueryBuilder import *
from rdflib import Graph, Namespace, Literal, RDF, URIRef, XSD, RDFS
from typing import Optional
import time
import csv
import requests
import json
import yaml
import shutil
from rdflib import Dataset
import subprocess
import multiprocessing  
import tempfile
import uuid


# ---------------------------------------------------------------------
# External configuration (paths, ontology URIs, filters, etc.)
# ---------------------------------------------------------------------

with open("./conf.yaml") as fh: 
    read_params = yaml.load(fh, Loader=yaml.FullLoader)

start_script = time.time()
path_to_vcd = read_params["vcd"]["vcd_path"]
vcd_files = [pos_vcd for pos_vcd in os.listdir(path_to_vcd) if pos_vcd.endswith('.json')]

# Ontology base URI and global dataset used to bind prefixes for bulk import
my_uri = read_params["ontology"]["my_uri"]
pref = read_params["ontology"]["pref"]
graph_context = read_params["ontology"]["graph"]
ns = Namespace(my_uri)


db_endpoint = read_params["database"]["db_endpoint"]
omit_CAN = read_params.get("data_to_omit", {}).get("CAN", [])
omit_IMU = read_params.get("data_to_omit", {}).get("IMU", [])
omit_ZOE = read_params.get("data_to_omit", {}).get("ZOE", [])
omit_relations = read_params.get("data_to_omit", {}).get("static_relations", [])


ds = Dataset()
ds.bind(pref, ns)
ds.bind("rdf", Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#"))
# Disable the global in-memory dataset to avoid accumulating triples across files.
USE_GLOBAL_DATASET = False

# Metrics
METRICS_HEADER = ["Scene", "Filename", "Filesize", "Time", "Nodes", "Relations", "Attributes", "Calls"]


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def add_scene_to_named_graph(scene_data, scene_relations, dataset, uri, pref, graph_name):
    
    """Add scene instances, their attributes, and explicit relations into a named graph."""
    
    g = dataset.graph(URIRef(graph_name))
    for instance, attributes in scene_data.items():
        subject = URIRef(f"{uri}{instance}")
        for predicate, values in attributes.items():
            if predicate == "rdf:type":
                if values is None:
                    continue
                if isinstance(values, list):
                    for v in values:
                        if isinstance(v, str):
                            obj = URIRef(v) if v.startswith(uri) else URIRef(uri + v)
                            g.add((subject, RDF.type, obj))
                else:
                    if isinstance(values, str):
                        obj = URIRef(values) if values.startswith(uri) else URIRef(uri + values)
                        g.add((subject, RDF.type, obj))
            else:
                pred = URIRef(f"{uri}{predicate}")
                if values is None:
                    continue
                if isinstance(values, list):
                    for value in values:
                        val_obj = _convert_value(value, uri)
                        if val_obj is not None:
                            g.add((subject, pred, val_obj))
                else:
                    val_obj = _convert_value(values, uri)
                    if val_obj is not None:
                        g.add((subject, pred, val_obj))
    # Relations
    for subject_name, rels in scene_relations.items():
        subject = URIRef(f"{uri}{subject_name}")
        for predicate, targets in rels.items():
            pred = URIRef(f"{uri}{predicate}")
            if isinstance(targets, list):
                for target in targets:
                    if isinstance(target, Literal):  
                        g.add((subject, pred, target))
                    else:  
                        g.add((subject, pred, URIRef(f"{uri}{target}")))
            else:
                if isinstance(targets, Literal):
                    g.add((subject, pred, targets))
                else: 
                    g.add((subject, pred, URIRef(f"{uri}{targets}")))


def convert_bytes(size):
    """Return a human-readable file size string."""
    for x in ['bytes', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return "%3.1f %s" % (size, x)
        size /= 1024.0
    return size

def count_relations(d):
    """Count how many relation targets exist within a nested dict/list structure."""
    count = 0
    if isinstance(d, dict):
        for key, value in d.items():
            if isinstance(value, list):
                count += len(value)
            elif isinstance(value, dict):
                count += count_relations(value)
    return count

def _convert_value(value, uri):
    """Convert Python values to RDF terms (URIRef/Literal) or skip if unsupported."""
    if value is None:
        return None
    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int):
        return Literal(value, datatype=XSD.integer)
    elif isinstance(value, float):
        return Literal(value, datatype=XSD.float)
    elif isinstance(value, str) and value.startswith(uri):
        return URIRef(value)
    elif isinstance(value, str):
        return Literal(value)
    else:
        return None


def write_metrics_csv(rows, path="Final.csv"):
    """Write metrics rows to CSV (single writer to avoid concurrency issues)."""
    with open(path, 'w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(METRICS_HEADER)
        writer.writerows(rows)
    
# -----------------------------------------------------------------------------
# Parser helpers
# -----------------------------------------------------------------------------


def fetch_categories():
    """Discover ontology classes grouped as Dynamic/Static/Movable via the ontology API."""
    Dyn = read_params["classes"]["Dynamic"]
    Mov = read_params["classes"].get("Movable", None)  # Synergies no longer uses it
    Stat = read_params["classes"]["Static"]

    categories = {"dynamic": set(), "static": set(), "movable": set()}
    try:
        api_cfg = read_params.get("api", {})
        classes_api_url = api_cfg.get(
            "classes_url",
            "http://localhost:8085/getClasses",
        )
        repo_name = api_cfg.get("repository_name") or read_params.get(
            "database",
            {},
        ).get("repository", "Synergies")
        ontology_pref = api_cfg.get("ontology_name") or read_params.get(
            "ontology",
            {},
        ).get("pref", "")
        ontology_uri = api_cfg.get("ontology_uri") or read_params.get(
            "ontology",
            {},
        ).get("my_uri", "")
        graphdb_url = api_cfg.get("graphdb_url") or read_params.get(
            "database",
            {},
        ).get("db_base_url")
        db_ip = read_params.get("database", {}).get("db_ip")
        db_port = read_params.get("database", {}).get("db_port")
        if not graphdb_url and db_ip and db_port:
            graphdb_url = f"http://{db_ip}:{db_port}"

        params = {"repository_name": repo_name}
        if ontology_pref:
            params["ontology_name"] = ontology_pref
        if ontology_uri:
            params["ontology_uri"] = ontology_uri
        if graphdb_url:
            params["graphdb_url"] = graphdb_url

        response = requests.get(
            url=classes_api_url,
            params=params,
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            for uri, details in data.items():
                parents = [p.strip() for p in details.get('Parents', "").split(",") if p.strip()]
                if Dyn in parents:
                    categories["dynamic"].add(details["Name"])
                if Stat in parents:
                    categories["static"].add(details["Name"])
                if Mov and Mov in parents:
                    categories["movable"].add(details["Name"])
        else:
            print(f"Error al obtener categorías: {response.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Error al conectar con la API: {e}")
    return categories


categories = fetch_categories()

# Load ontology to derive subproperties ordering
ontology_path = read_params["ontology"]["ontology_path"]
g_ontology = Graph()
g_ontology.parse(ontology_path)


def get_subproperties_in_order(property_name):
    """Return subproperties of a property following their order of appearance in the ontology."""
    property_uri = URIRef(f"{my_uri}{property_name}")
    subproperties = []
    for sp in g_ontology.subjects(RDFS.subPropertyOf, property_uri):
        subproperties.append(sp)
    subproperties_in_order = []
    for s, p, o in g_ontology.triples((None, RDF.type, None)):
        if s in subproperties:
            subproperties_in_order.append(s)
    subproperties_names = [str(sp).split('#')[-1] for sp in subproperties_in_order]
    return subproperties_names

# -----------------------------------------------------------------------------
# VCD processing
# -----------------------------------------------------------------------------

def process_vcd(v):
    """Parse one VCD, build per-scene RDF, and serialize to a temp .nq."""
    start_file = time.time()
    scene_data = {}
    scene_relations = {}
    object_dic = {}
    object_positions_dic = {}
    ego_vehicle = None
    imu = True
    zoe = True

    # Load VCD
    print(f"\nProcesando archivo VCD: {v}")
    myVCD = core.VCD()
    myVCD.load_from_file(os.path.join(path_to_vcd, v))


    # -----------------------------------------------------------------------------
    # Conf. from YAML
    # -----------------------------------------------------------------------------

    # EGO VEHICLE
    ego_vehicle_name = read_params.get("ego_vehicle", {}).get("ego_vehicle_name", None)
    ego_rotation_name = read_params.get("ego_vehicle", {}).get("ego_rotation", None)

    # CLASSES
    Scene_class = read_params.get("classes", {}).get("Scene", None)
    EgoData_class = read_params.get("classes", {}).get("EgoData", None)
    ObjectData_class = read_params.get("classes", {}).get("ObjectData", None)
    Lane_class = read_params.get("classes", {}).get("Lane", None)
    Followed_class = read_params.get("classes", {}).get("Followed", None)

    # RELATIONS
    hasEgoData_relation = read_params.get("relations", {}).get("hasEgoData", None)
    hasData_relation = read_params.get("relations", {}).get("hasData", None)
    hasObject_relation = read_params.get("relations", {}).get("hasObject", None)
    isLocatedIn_relation = read_params.get("relations", {}).get("isLocatedIn", None)
    isNextTo_relation = read_params.get("relations", {}).get("isNextTo", None)
    isPartOf_relation = read_params.get("relations", {}).get("isPartOf", "isPartOf")
    hasIncomingLane_relation = read_params.get("relations", {}).get("hasIncomingLane", None)
    hasOutgoingLane_relation = read_params.get("relations", {}).get("hasOutgoingLane", None)
    participatesIn_relation = read_params.get("relations", {}).get("participatesIn", None)
    hasVCDnumber_relation = read_params.get("relations", {}).get("hasUIDinVCD", None)
    enable_is_part_of = bool(isPartOf_relation)

    # -----------------------------------------------------------------------------

    # Scene metadata and target named graph
    scene_metadata = myVCD.get_metadata()
    scene_name = scene_metadata.get('scene_name')
    scene_description = scene_metadata.get('scene_description')
    scene_graph = graph_context + scene_name


    scene_data[scene_name] = {'rdf:type': Scene_class, 'scene_description': scene_description} 
    scene_relations[scene_name] = {hasObject_relation: [], hasEgoData_relation: []} 
    scene_relations[ego_vehicle_name] = {hasData_relation: []} 


    # Objects pass: create instances and per-frame nodes for dynamic entities
    object_dict = myVCD.get_objects()
    static_types = {}
    for obj_uid, obj_data in object_dict.items():
        instance_name = obj_data.get('name')
        instance_type = obj_data.get('type')

        if instance_name == ego_vehicle_name: 
            scene_data[ego_vehicle_name] = {'rdf:type': instance_type}
            ego_vehicle = obj_uid
            object_positions_dic[ego_vehicle_name] = []
            frame_intervals = [
                (interval['frame_start'], interval['frame_end'])
                for interval in obj_data.get('frame_intervals', [])
                if 'frame_start' in interval and 'frame_end' in interval
            ]

            scene_relations.setdefault(ego_vehicle_name, {})
            obj_uid_str = str(obj_uid)
            scene_relations[ego_vehicle_name].setdefault(hasVCDnumber_relation, []).append(Literal(obj_uid_str, datatype=XSD.string))

            for frame_start, frame_end in frame_intervals:
                for frame_id in range(frame_start, frame_end + 1):
                    frame_instance = f"{scene_name}_ego_frame_{frame_id}"
                    # Create a node per frame to attach time-varying attributes
                    scene_data[frame_instance] = {'rdf:type': EgoData_class, 'framestamp': frame_id} 
                    scene_relations[ego_vehicle_name][hasData_relation].append(frame_instance) 
                    scene_relations[scene_name][hasEgoData_relation].append(frame_instance) 
                    scene_relations[frame_instance] = {isLocatedIn_relation: []} 
                    object_positions_dic[ego_vehicle_name].append(frame_instance)

        elif instance_type in categories["dynamic"] or instance_type in categories["movable"]: 
            frame_intervals = [
                (interval['frame_start'], interval['frame_end'])
                for interval in obj_data.get('frame_intervals', [])
                if 'frame_start' in interval and 'frame_end' in interval
            ]
            if frame_intervals:
                scene_data[instance_name] = {'rdf:type': instance_type}
                scene_relations[instance_name] = {hasData_relation: []}
                object_positions_dic[instance_name] = []
                object_dic[obj_uid] = instance_name
                scene_relations[scene_name][hasObject_relation].append(instance_name)

                scene_relations.setdefault(instance_name, {})
                obj_uid_str = str(obj_uid)
                scene_relations[instance_name].setdefault(hasVCDnumber_relation, []).append(Literal(obj_uid_str, datatype=XSD.string))

                for frame_start, frame_end in frame_intervals:
                    for frame_id in range(frame_start, frame_end + 1):
                        frame_instance = f"{instance_name}_frame_{frame_id}"
                        scene_data[frame_instance] = {'rdf:type': ObjectData_class, 'framestamp': frame_id} 
                        scene_relations[instance_name][hasData_relation].append(frame_instance)
                        scene_relations[frame_instance] = {isLocatedIn_relation: []}  
                        object_positions_dic.setdefault(instance_name, []).append(frame_instance) 

        elif instance_type in categories["static"]: 
                    
            scene_data[instance_name] = {'rdf:type': instance_type}
            scene_relations[scene_name][hasObject_relation].append(instance_name) 
            object_dic[obj_uid] = instance_name
            static_types[instance_name] = instance_type

            scene_relations.setdefault(instance_name, {})
            obj_uid_str = str(obj_uid)
            scene_relations[instance_name].setdefault(hasVCDnumber_relation, []).append(Literal(obj_uid_str, datatype=XSD.string))
            
            # Lanes may carry structural relations if not omitted
            if instance_type == Lane_class:
                scene_relations[instance_name].setdefault(isNextTo_relation, [])
                if hasIncomingLane_relation not in omit_relations:
                    scene_relations[instance_name][hasIncomingLane_relation] = []
                if hasOutgoingLane_relation not in omit_relations:
                    scene_relations[instance_name][hasOutgoingLane_relation] = []

        else:
            print(f"Advertencia: Tipo de objeto estático desconocido '{instance_type}'. No se procesará.")

    if enable_is_part_of:
        def _extract_section_name(name: str) -> Optional[str]:
            idx = name.find("_SECTION")
            if idx == -1:
                return None
            next_sep = name.find("_", idx + len("_SECTION"))
            return name if next_sep == -1 else name[:next_sep]

        def _extract_road_name(name: str) -> Optional[str]:
            idx = name.find("_ROAD")
            if idx == -1:
                return None
            next_sep = name.find("_", idx + len("_ROAD"))
            return name if next_sep == -1 else name[:next_sep]

        section_children = {
            "LANE",
            "SHOULDER",
            "SIDEWALK",
            "PARKING",
            "MEDIAN",
            "BORDER",
            "NONE",
            "BIDIRECTIONAL",
        }

        for child_name, child_type in static_types.items():
            parent_name = None
            if child_type == "SECTION":
                parent_name = _extract_road_name(child_name)
            elif child_type in section_children:
                parent_name = _extract_section_name(child_name)

            if not parent_name or parent_name not in static_types:
                continue

            rels = scene_relations.setdefault(child_name, {})
            rel_list = rels.setdefault(isPartOf_relation, [])
            if parent_name not in rel_list:
                rel_list.append(parent_name)

    # Collect available frame IDs
    frames = []
    frame_id = 0

    while True:
        frame = myVCD.get_frame(frame_id)
        if frame is None:  
            break
        frames.append(frame_id)  
        frame_id += 1 

    attributes_to_include = read_params.get("attributes_to_include", {})
    frame_positions_dic = {}
    for frame_id in frames:
        frame_positions_dic[frame_id] = []
        # Timestamp and ego transform extraction per frame
        frame_properties = myVCD.get_frame(frame_id).get('frame_properties', {})

        # --- safe timestamp ---
        timestamp = None
        timestamp_val = frame_properties.get('timestamp')
        if timestamp_val is not None:
            try:
                timestamp = int(timestamp_val)
            except (TypeError, ValueError):
                timestamp = None  # por si viene string raro

        # --- safe ego rotation ---
        ego_rot = None
        transforms = frame_properties.get('transforms', {}).get('vehicle-iso8855_to_odom', {})
        if isinstance(transforms, dict) and 'odometry_xyzypr' in transforms:
            ego_rot = transforms['odometry_xyzypr'][3]

        frame_objects = myVCD.get_frame(frame_id).get('objects', {})
        for obj_uid, obj_data in frame_objects.items():
            if obj_uid != ego_vehicle:
                # Non-ego objects: attach attributes to their per-frame nodes
                if 'object_data' in obj_data and obj_data['object_data']:
                    frame_instance = f"{object_dic[obj_uid]}_frame_{frame_id}"
                    if frame_instance in scene_data:
                        scene_data[frame_instance].update({'timestamp': timestamp})
                    else:
                        continue

                    if frame_instance not in scene_relations:
                        scene_relations[frame_instance] = {isLocatedIn_relation: []}                  
                    region_dict = {}

                    for data_category, data_items in obj_data['object_data'].items():
                        for data_item in data_items:
                            attribute_name = data_item['name']
                            attribute_value = data_item['val']
                            
                            # Only include attributes whitelisted in conf
                            if attribute_name in attributes_to_include.get(data_category, set()): 

                                if data_category == 'boolean':
                                    region_dict[attribute_name] = attribute_value                                    
                                elif data_category == 'cuboid':
                                    # Expand structured attributes using subproperty order
                                    subproperties = get_subproperties_in_order(attribute_name)
                                    if frame_instance in scene_data:
                                        scene_data[frame_instance].update({
                                            subproperties[0]: attribute_value[0],
                                            subproperties[1]: attribute_value[1],
                                            subproperties[2]: attribute_value[2],
                                            subproperties[3]: attribute_value[5]
                                        })
                                        frame_positions_dic[frame_id].append(frame_instance)
                                        object_positions_dic[object_dic[obj_uid]].append(frame_instance)
                                    else:
                                        continue

                                elif data_category == 'point3d':
                                    subproperties = get_subproperties_in_order(attribute_name)
                                    if frame_instance in scene_data:
                                        scene_data[frame_instance].update({
                                            subproperties[0]: attribute_value[0],
                                            subproperties[1]: attribute_value[1],
                                            subproperties[2]: attribute_value[2]
                                        })
                                    else:
                                        continue

                                elif data_category == 'vec':
                                    subproperties = get_subproperties_in_order(attribute_name)
                                    if frame_instance in scene_data:
                                        scene_data[frame_instance].update({
                                            subproperties[0]: attribute_value[0],
                                            subproperties[1]: attribute_value[1],
                                            subproperties[2]: attribute_value[2]
                                        })
                                    else:
                                        continue

                                elif data_category == 'num':
                                    if frame_instance in scene_data:
                                        scene_data[frame_instance].update({attribute_name: attribute_value})
                                    else:
                                      continue

                                elif data_category == 'text':
                                    if frame_instance in scene_data:
                                        scene_data[frame_instance].update({attribute_name: attribute_value})
                                    else:
                                      continue
                    if frame_instance in scene_data:
                        scene_data[frame_instance].update(region_dict)
                    else:
                        continue

            else:
                # Ego vehicle: attach ego-specific attributes to the ego per-frame node
                frame_instance = f"{scene_name}_ego_frame_{frame_id}"
                scene_data[frame_instance].update({ego_rotation_name: ego_rot, 'timestamp': timestamp}) 

                if frame_instance not in scene_relations:
                    scene_relations[frame_instance] = {isLocatedIn_relation: []}
                obj_data = myVCD.get_frame(frame_id)['objects'][ego_vehicle]

                if 'object_data' in obj_data and obj_data['object_data']:
                    for data_category, data_items in obj_data['object_data'].items():
                        if data_category != 'cuboid':  
                            for data_item in data_items:
                                attribute_name = data_item['name']
                                attribute_value = data_item['val']

                                if attribute_name in attributes_to_include.get(data_category, set()):

                                    if data_category == 'point3d':
                                        subproperties = get_subproperties_in_order(attribute_name)
                                        scene_data[frame_instance].update({
                                            subproperties[0]: attribute_value[0],
                                            subproperties[1]: attribute_value[1],
                                            subproperties[2]: attribute_value[2]
                                        })
                                        frame_positions_dic[frame_id].append(frame_instance)
                                        object_positions_dic[ego_vehicle_name].append(frame_instance)
                                    
                                    elif data_category == 'vec':
                                        subproperties = get_subproperties_in_order(attribute_name)
                                        scene_data[frame_instance].update({
                                            subproperties[0]: attribute_value[0],
                                            subproperties[1]: attribute_value[1],
                                            subproperties[2]: attribute_value[2]
                                        })

                                    elif data_category == 'num':
                                        scene_data[frame_instance].update({attribute_name: attribute_value})

                                    elif data_category == 'text':
                                        scene_data[frame_instance].update({attribute_name: attribute_value})

                                else:
                                    if attribute_name not in omit_CAN:
                                            scene_data[frame_instance].update({attribute_name: attribute_value})


    # Relations pass: link positions to container regions and other structural links
    relations = myVCD.get_relations()
    if relations:
        for relation_id, relation_data in relations.items():
            subject_uid = relation_data['rdf_subjects'][0]['uid']
            object_uid = relation_data['rdf_objects'][0]['uid']
            relation_type = relation_data['type']

            if relation_type == isLocatedIn_relation: 
                if 'frame_intervals' in relation_data:
                    frame_intervals = relation_data['frame_intervals']
                    for interval in frame_intervals:
                        frame_start = interval['frame_start']
                        frame_end = interval['frame_end']

                        for frame_id in range(frame_start, frame_end + 1):
                            frame_positions = frame_positions_dic.get(frame_id, [])

                            if subject_uid != ego_vehicle:
                                subject_name = object_dic.get(subject_uid)
                                object_positions = object_positions_dic.get(subject_name, [])
                            else:
                                object_positions = object_positions_dic.get(ego_vehicle_name, [])
                        
                            if frame_positions and object_positions:
                                intersection = list(set(frame_positions).intersection(object_positions))
                                if intersection:
                                    for position in intersection:
                                        if position not in scene_relations:
                                            scene_relations[position] = {isLocatedIn_relation: []}

                                        scene_relations[position].setdefault(isLocatedIn_relation, []).append(object_dic[object_uid])

            elif relation_type not in omit_relations:
                if subject_uid in object_dic:
                        subject_name = object_dic[subject_uid]
                        if subject_name not in scene_relations:
                            scene_relations[subject_name] = {}
                        scene_relations[subject_name].setdefault(relation_type, []).append(object_dic[object_uid])
                else:
                    print(f"Warning: subject_uid {subject_uid} not found in object_dic. Skipping relation.")

    # Remove empty entries in relations structure
    def clean_empty(d):
        if isinstance(d, dict):
            return {k: clean_empty(v) for k, v in d.items() if v}
        elif isinstance(d, list):
            return [clean_empty(v) for v in d if v]
        else:
            return d
    scene_relations = clean_empty(scene_relations)
    
    if USE_GLOBAL_DATASET:
        add_scene_to_named_graph(scene_data, scene_relations, ds, my_uri, pref, scene_graph)
    
    time_int = time.time() - start_file
    calls = 0  
    nodes = len(scene_data)
    rel = count_relations(scene_relations)
    attr = sum(len(v) for v in scene_data.values()) - nodes
    f_size = os.path.getsize(os.path.join(path_to_vcd, v))
    size = convert_bytes(f_size)
    metrics = [scene_name, v, size, time_int, nodes, rel, attr, calls]

    tmp_file = os.path.join(tempfile.mkdtemp(), f"{uuid.uuid4().hex}.nq")
    ds_local = Dataset()
    ds_local.bind(pref, ns)
    ds_local.bind("rdf", Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#"))

    add_scene_to_named_graph(scene_data, scene_relations, ds_local, my_uri, pref, scene_graph)
    ds_local.serialize(destination=tmp_file, format='nquads')
    
    return tmp_file, metrics

def merge_results(queue):
    """Function to merge scene fragments from a multiprocessing queue into the global dataset."""
    if not USE_GLOBAL_DATASET:
        return
    global ds
    while not queue.empty():
        scene_data, scene_relations = queue.get()
        add_scene_to_named_graph(scene_data, scene_relations, ds, my_uri, pref, graph_context)

def parse_args():
    parser = argparse.ArgumentParser(description="Preload VCDs and generate N-Quads output")
    parser.add_argument(
        "--mode",
        choices=("mp", "seq"),
        default="mp",
        help="Processing mode: mp (multiprocessing) or seq (sequential).",
    )
    parser.add_argument(
        "--processes",
        type=int,
        default=None,
        help="Number of worker processes for mp mode (default: os.cpu_count()).",
    )
    return parser.parse_args()


def append_chunk(dst_handle, chunk_path):
    with open(chunk_path, 'rb') as src:
        shutil.copyfileobj(src, dst_handle)


def run_multiprocessing(output_file, processes):
    with multiprocessing.Pool(processes=processes) as pool:
        results = pool.map(process_vcd, vcd_files)

    metrics_rows = []
    with open(output_file, 'wb') as dst:
        for chunk_path, metrics in results:
            append_chunk(dst, chunk_path)
            os.remove(chunk_path)
            metrics_rows.append(metrics)

    return metrics_rows


def run_sequential(output_file):
    metrics_rows = []
    with open(output_file, 'wb') as dst:
        for vcd in vcd_files:
            chunk_path, metrics = process_vcd(vcd)
            append_chunk(dst, chunk_path)
            os.remove(chunk_path)
            metrics_rows.append(metrics)
    return metrics_rows


if __name__ == '__main__':
    args = parse_args()
    output_file = read_params.get("outputs", {}).get("nq_file", "Synergies.nq")
    output_dir = os.path.dirname(output_file)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    if args.mode == "seq":
        metrics_rows = run_sequential(output_file)
    else:
        metrics_rows = run_multiprocessing(output_file, args.processes)

    write_metrics_csv(metrics_rows)

    print(f"Dataset serializado en {output_file}")
    print('It took', time.time() - start_script, 'seconds.')
    print('All temporary files have been removed.')


#nohup python3 mi_script.py &
