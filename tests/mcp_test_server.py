"""测试用 MCP stdio 服务器：pytest 以子进程方式拉起。"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("testsrv")


@mcp.tool()
def echo(text: str) -> str:
    """原样返回输入文本"""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """两数相加"""
    return a + b


if __name__ == "__main__":
    mcp.run(transport="stdio")
