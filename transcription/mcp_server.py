"""Standalone MCP server exposing the same file-reading tools used by LangGraph.

Run with: python -m transcription.mcp_server
"""
from mcp.server.fastmcp import FastMCP

from . import mcp_tools
from .settings import Settings
from .logging_config import configure_logging

mcp = FastMCP("transcription-audio-reader")
@mcp.tool()
def inspect_audio_file(path: str) -> dict:
    """Read metadata for a configured media format."""
    return mcp_tools.inspect_audio_file(path, Settings.load().asr_supported_format)


@mcp.tool()
def list_audio_files(directory: str) -> list[dict]:
    """List media files matching asr.supported_format and their metadata."""
    return mcp_tools.list_audio_files(directory, Settings.load().asr_supported_format)


if __name__ == "__main__":
    configure_logging()
    mcp.run(transport="stdio")
