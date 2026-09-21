"""
Confine transazionale condiviso dai Service.

Estratto quando il `PaymentService` ha avuto bisogno della stessa identica
logica già presente nel `BookingService`: due copie sarebbero divergute, e
questa in particolare non può divergere — una svista qui non produce un errore
ma una scrittura persa in silenzio, come già successo allo Step E.
"""
from typing import Awaitable, Callable, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


async def run_in_transaction(
        session: AsyncSession,
        operation: Callable[[], Awaitable[T]]
) -> T:
    """
    Esegue l'operazione come unità atomica: commit se va a buon fine,
    rollback a fronte di qualunque eccezione.

    ⚠️ **Non si usa `session.in_transaction()` per decidere se aprire una
    transazione.** SQLAlchemy 2.0 ha l'autobegin: una transazione si apre da
    sola alla **prima query**, anche di sola lettura. La dipendenza di
    autenticazione interroga la tabella utenti prima ancora di entrare
    nell'endpoint, quindi su ogni rotta protetta la sessione risulta già "in
    transazione". Il pattern `if in_transaction(): ... else: begin()`
    concludeva perciò che il commit spettasse a qualcun altro — e la scrittura
    non veniva mai persistita. Silenziosamente: la risposta HTTP era `201`, ma
    a database non restava nulla.

    Qui la transazione viene chiusa esplicitamente, senza ipotesi su chi
    l'abbia aperta.
    """
    try:
        result = await operation()
        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise
