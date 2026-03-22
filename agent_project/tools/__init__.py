"""Tool registry, LLM instances, and runtime constants."""

import os

import dotenv
from langchain_openai import ChatOpenAI

from .calculator import calculator
from .fetch_url import fetch_url
from .python_exec import execute_python
from .retrieval import retrieve_context, retrieve_tool_result
from .search import search_web

dotenv.load_dotenv()

MAX_TOOL_ROUNDS = 10
MAX_SEARCHES_PER_STEP = 3
SHOW_TOKEN_STREAM = False

TOOLS = [calculator, search_web, fetch_url, retrieve_context, retrieve_tool_result, execute_python]
TOOLS_BY_NAME = {t.name: t for t in TOOLS}

llm = ChatOpenAI(model="gpt-5-nano", api_key=os.getenv("OPENAI_API_KEY"))
agent_llm = llm.bind_tools(TOOLS)
