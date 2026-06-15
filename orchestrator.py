import operator
import os
import sys
from typing import TypedDict

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field
from typing_extensions import Annotated

load_dotenv()
os.environ["GROQ_API_KEY"] = os.getenv("GROQ_API_KEY")
sys.stdout.reconfigure(encoding="utf-8")

llm = ChatGroq(model="openai/gpt-oss-120b", max_tokens=500)


# ---------------------------------------------------------------------------
# Orchestrator workflow idea
# ---------------------------------------------------------------------------
# Normal graph:
#   START -> node_1 -> node_2 -> END
#
# Parallel graph:
#   START -> node_a -> END
#   START -> node_b -> END
#
# Orchestrator-worker graph:
#   START -> orchestrator
#                |
#                | creates a dynamic list of worker jobs
#                v
#            Send("worker", job_1)
#            Send("worker", job_2)
#            Send("worker", job_3)
#                |
#                v
#            synthesizer -> END
#
# The key thing:
# The orchestrator decides HOW MANY worker calls are needed at runtime.
# This is why we use LangGraph's Send API.


class ReportPart(BaseModel):
    title: str = Field(description="Title of this report part.")
    brief: str = Field(description="What this report part should explain.")


class ReportPlan(BaseModel):
    parts: list[ReportPart] = Field(description="Parts needed for the report.")


class State(TypedDict):
    topic: str
    report_plan: list[ReportPart]

    # Multiple workers will return draft_parts at the same time.
    # operator.add tells LangGraph:
    # "Do not overwrite the list. Add all returned lists together."
    draft_parts: Annotated[list[str], operator.add]

    final_report: str


class WorkerState(TypedDict):
    topic: str
    assigned_part: ReportPart

    # Worker returns this key back into the main State.
    draft_parts: Annotated[list[str], operator.add]


def orchestrator(state: State):
    """Break the user's topic into a dynamic report plan."""

    planner = llm.with_structured_output(ReportPlan)

    response = planner.invoke(
        [
            SystemMessage(
                content=(
                    "You are a planning assistant. Break the user's topic into "
                    "3 clear report parts. Keep each part brief."
                )
            ),
            HumanMessage(content=f"Topic: {state['topic']}"),
        ]
    )

    return {"report_plan": response.parts}


def assign_workers(state: State):
    """Use Send API to dynamically run one worker for each planned report part."""

    return [
        Send(
            "worker",
            {
                "topic": state["topic"],
                "assigned_part": report_part,
            },
        )
        for report_part in state["report_plan"]
    ]


def worker(state: WorkerState):
    """Write one planned part of the report."""

    report_part = state["assigned_part"]

    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are a helpful technical writer. Write one concise, "
                    "beginner-friendly report part."
                )
            ),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Part title: {report_part.title}\n"
                    f"Part brief: {report_part.brief}"
                )
            ),
        ]
    )

    draft_part = f"## {report_part.title}\n\n{response.content}"

    # Return a list because draft_parts uses operator.add.
    return {"draft_parts": [draft_part]}


def synthesizer(state: State):
    """Combine all worker outputs into one final report."""

    joined_draft_parts = "\n\n".join(state["draft_parts"])

    response = llm.invoke(
        [
            SystemMessage(
                content=(
                    "You are an editor. Combine the draft parts into one polished "
                    "report. Keep it readable for a beginner."
                )
            ),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n\n"
                    f"Draft parts written by workers:\n\n{joined_draft_parts}"
                )
            ),
        ]
    )

    return {"final_report": response.content}


graph = StateGraph(State)

graph.add_node("orchestrator", orchestrator)
graph.add_node("worker", worker)
graph.add_node("synthesizer", synthesizer)

graph.add_edge(START, "orchestrator")

# Conditional edge means:
# After orchestrator runs, call assign_workers().
# assign_workers() returns Send objects.
# Each Send object runs the worker node with its own small state.
graph.add_conditional_edges("orchestrator", assign_workers, ["worker"])

# Every worker goes to synthesizer after finishing.
# LangGraph waits for the worker outputs and merges draft_parts.
graph.add_edge("worker", "synthesizer")
graph.add_edge("synthesizer", END)

builder = graph.compile()

# Send can run workers in parallel. Groq free/on-demand tiers may hit TPM limits
# when many workers call the model at the same time, so this demo keeps
# concurrency gentle. Remove config={"max_concurrency": 1} if your limit is high.
response = builder.invoke(
    {"topic": "Neural networks"},
    config={"max_concurrency": 1},
)

print("\nPLANNED REPORT PARTS")
for report_part in response["report_plan"]:
    print(f"- {report_part.title}: {report_part.brief}")

print("\nFINAL REPORT")
print(response["final_report"])



