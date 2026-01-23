from typing import Optional

from fastapi import APIRouter, Query

from app.services.classes_method import get_classes as get_classes_service

router = APIRouter()


@router.get("/health", tags=["health"])
def health():
    return {"status": "ok"}


@router.get("/getClasses", tags=["ontology"])
def get_classes(
    repository_name: str = Query(..., min_length=1),
    ontology_name: Optional[str] = Query(None),
    ontology_uri: Optional[str] = Query(None),
    graphdb_url: str = Query(..., min_length=1),
):
    return get_classes_service(
        repository_name=repository_name,
        ontology_name=ontology_name,
        ontology_uri=ontology_uri,
        graphdb_url=graphdb_url,
    )
