from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import RequestContext, verify_request
from app.db.session import get_db
from app.schemas import DocumentExtractionRequest, DocumentExtractionResponse
from app.services.document_extractions import DocumentExtractionService


router = APIRouter()


@router.get('/ready')
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text('SELECT 1'))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Service unavailable',
        ) from exc
    return {'status': 'ready'}


def get_document_extraction_service() -> DocumentExtractionService:
    return DocumentExtractionService()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/document-extractions/process", response_model=DocumentExtractionResponse)
def process_document_extraction(
    payload: DocumentExtractionRequest,
    context: RequestContext = Depends(verify_request),
    service: DocumentExtractionService = Depends(get_document_extraction_service),
) -> DocumentExtractionResponse:
    return service.process(payload, context)


@router.post("/invoices/extract", response_model=DocumentExtractionResponse)
def extract_invoice(
    payload: DocumentExtractionRequest,
    context: RequestContext = Depends(verify_request),
    service: DocumentExtractionService = Depends(get_document_extraction_service),
) -> DocumentExtractionResponse:
    return service.process(payload, context)
