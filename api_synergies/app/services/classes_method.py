import re
from typing import Dict, Optional

import requests
from fastapi import HTTPException
from SPARQLWrapper import JSON, SPARQLWrapper

from app.core.config import settings

_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _parse_namespaces_text(payload: str) -> Dict[str, str]:
    namespaces = {}
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if "\t" in line:
            prefix, namespace = line.split("\t", 1)
        elif "=" in line:
            prefix, namespace = line.split("=", 1)
        else:
            continue
        prefix = prefix.strip()
        namespace = namespace.strip()
        if prefix and namespace:
            namespaces[prefix] = namespace
    return namespaces


def _fetch_namespaces(base_url: str, repository_name: str, timeout: float) -> Dict[str, str]:
    url = f"{base_url}/repositories/{repository_name}/namespaces"
    headers = {"Accept": "application/sparql-results+json, application/json, text/plain"}
    response = requests.get(url, headers=headers, timeout=timeout)
    if not response.ok:
        raise HTTPException(
            status_code=response.status_code,
            detail=f"Failed to fetch namespaces: {response.text}",
        )

    content_type = response.headers.get("Content-Type", "")
    if "json" in content_type:
        try:
            data = response.json()
        except ValueError:
            data = None
        if isinstance(data, dict):
            namespaces = {}
            for item in data.get("results", {}).get("bindings", []):
                prefix = item.get("prefix", {}).get("value")
                namespace = item.get("namespace", {}).get("value")
                if prefix and namespace:
                    namespaces[prefix] = namespace
            if namespaces:
                return namespaces

    return _parse_namespaces_text(response.text)


def _build_query(ontology_name: Optional[str], ontology_uri: str) -> str:
    prefixes = [
        "PREFIX owl: <http://www.w3.org/2002/07/owl#>",
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
    ]

    use_prefix = bool(ontology_name and _PREFIX_RE.match(ontology_name))
    if use_prefix:
        prefixes.append(f"PREFIX {ontology_name}: <{ontology_uri}>")
        filter_clause = f"FILTER(STRSTARTS(STR(?class), STR({ontology_name}:)))"
    else:
        filter_clause = f'FILTER(STRSTARTS(STR(?class), "{ontology_uri}"))'

    prefix_block = "\n".join(prefixes)
    return f"""
{prefix_block}

SELECT DISTINCT ?class ?name ?label ?description
  (GROUP_CONCAT(?childLabel; separator=", ") as ?children)
  (GROUP_CONCAT(?parentLabel; separator=", ") as ?parents)
WHERE {{
  ?class a owl:Class ;
         rdfs:label ?label .
  {filter_clause}
  OPTIONAL {{ ?class rdfs:comment ?description . }}
  BIND(REPLACE(STR(?class), ".*[/#]", "") AS ?name)

  {{
    ?child a owl:Class ;
           rdfs:label ?childLabel ;
           rdfs:subClassOf ?class .
    FILTER NOT EXISTS {{
      ?child rdfs:subClassOf ?grandChild .
      ?grandChild rdfs:subClassOf ?class .
      FILTER (?child != ?grandChild)
    }}
  }}
  UNION
  {{
    ?class rdfs:subClassOf ?parent .
    ?parent rdfs:label ?parentLabel .
  }}
}}
GROUP BY ?class ?description ?name ?label
"""


def get_classes(
    repository_name: str,
    ontology_name: Optional[str],
    ontology_uri: Optional[str],
    graphdb_url: str,
):
    if not ontology_name and not ontology_uri:
        raise HTTPException(
            status_code=400,
            detail="ontology_name or ontology_uri is required.",
        )

    base_url = _normalize_base_url(graphdb_url)
    timeout = settings.GRAPHDB_TIMEOUT_SEC

    if ontology_name and not ontology_uri:
        namespaces = _fetch_namespaces(base_url, repository_name, timeout)
        ontology_uri = namespaces.get(ontology_name)
        if not ontology_uri:
            raise HTTPException(
                status_code=400,
                detail="Selected ontology does not exist in repository. Try changing the ontology name.",
            )

    query = _build_query(ontology_name, ontology_uri)
    endpoint = f"{base_url}/repositories/{repository_name}"
    sparql = SPARQLWrapper(endpoint)
    sparql.setQuery(query)
    sparql.setReturnFormat(JSON)

    try:
        results = sparql.query().convert()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GraphDB query failed: {exc}")

    class_dict: Dict[str, Dict[str, str]] = {}
    for result in results.get("results", {}).get("bindings", []):
        class_uri = result["class"]["value"]
        class_name = result.get("name", {}).get("value", "")
        class_label = result.get("label", {}).get("value", "")
        class_description = result.get("description", {}).get("value", "")
        class_parents = result.get("parents", {}).get("value", "")
        class_children = result.get("children", {}).get("value", "")

        class_dict[class_uri] = {
            "URI": class_uri,
            "Name": class_name,
            "Label": class_label,
            "Description": class_description,
            "Parents": class_parents,
            "Children": class_children,
        }

    return class_dict
