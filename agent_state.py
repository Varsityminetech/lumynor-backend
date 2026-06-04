from typing import TypedDict, List, Annotated
import operator
from pydantic import BaseModel, Field

class AgentMessage(BaseModel):
    department: str
    sender: str
    message: str

class CompanyState(TypedDict):
    current_department: str
    chat_history: Annotated[List[dict], operator.add]
    current_document: str
    pending_approval: bool

