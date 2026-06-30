from pydantic import BaseModel, Field
from typing import Optional, Any, Literal


class QueryRequest(BaseModel):
    question:    str           = Field(..., min_length=2, max_length=500)
    language:    str           = Field("en", description="en | hi")
    session_id:  Optional[str] = None
    include_sql: bool          = False
    # "cross_scheme" when the UI is in Cross Scheme mode — the backend then treats EVERY question as
    # spanning Focus / Focus+ / CM Elevate, widens table scope, and avoids the single-scheme refusal.
    mode:        Optional[str] = None


class QueryResponse(BaseModel):
    question:          str
    answer:            str
    intent:            str                                            = "SQL"
    data:              Optional[list[dict[str, Any]]]                 = None
    sql_query:         Optional[str]                                  = None
    row_count:         int                                            = 0
    execution_time_ms: int                                            = 0
    confidence:        str                                            = "high"
    chart_type:        Optional[Literal["bar", "line", "doughnut", "grouped_bar", "stacked", "kpi_bar"]] = None
    edge_type:         Optional[str]                                  = None
    follow_up:         Optional[str]                                  = None
