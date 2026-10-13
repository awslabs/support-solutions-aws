# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# ruff: noqa: D101, D102, D103
"""Tests for the Well-Architected Review MCP Server for Amazon SageMaker."""

import argparse
import pytest
from well_architected_review_mcp_server_for_sagemaker.wa_validation_handler import WellArchitectedValidationHandler
from unittest.mock import MagicMock, patch


@pytest.mark.asyncio
async def test_server_initialization():
    from well_architected_review_mcp_server_for_sagemaker.server import create_server

    server = create_server()

    assert server.name == 'well-architected-review-mcp-server-for-sagemaker'
    assert server.instructions is not None
    assert 'Well-Architected Review MCP Server for Amazon SageMaker' in server.instructions
    assert 'pydantic' in server.dependencies
    assert 'loguru' in server.dependencies
    assert 'boto3' in server.dependencies


@pytest.mark.asyncio
async def test_server_initialization_streamable_http_sets_host_port():
    """streamable-http transport must bind 0.0.0.0:8000 in stateless mode per the
    Amazon Bedrock AgentCore Runtime MCP protocol contract."""
    from well_architected_review_mcp_server_for_sagemaker.server import create_server

    server = create_server(transport='streamable-http')

    assert server.settings.host == '0.0.0.0'
    assert server.settings.port == 8000
    assert server.settings.stateless_http is True


@pytest.mark.asyncio
async def test_server_initialization_stdio_has_no_network_binding():
    """stdio transport (default/local use) must not set network-only settings."""
    from well_architected_review_mcp_server_for_sagemaker.server import create_server

    server = create_server(transport='stdio')

    # FastMCP defaults host/port even when unused by stdio; the behavior we
    # guard is that stdio does NOT force stateless_http on, since that setting
    # only has meaning for HTTP transports.
    assert server.settings.stateless_http is False


@pytest.mark.asyncio
async def test_command_line_args_default():
    """Test default args (no sensitive data access)."""
    from well_architected_review_mcp_server_for_sagemaker.server import main

    with patch.object(argparse.ArgumentParser, 'parse_args') as mock_parse_args:
        mock_parse_args.return_value = argparse.Namespace(
            allow_sensitive_data_access=False, transport='stdio'
        )

        mock_server = MagicMock()
        with patch(
            'well_architected_review_mcp_server_for_sagemaker.server.create_server', return_value=mock_server
        ):
            with patch(
                'well_architected_review_mcp_server_for_sagemaker.server.WellArchitectedValidationHandler'
            ) as mock_handler:
                main()

                mock_parse_args.assert_called_once()
                mock_handler.assert_called_once_with(mock_server, False)
                mock_server.run.assert_called_once_with(transport='stdio')


@pytest.mark.asyncio
async def test_command_line_args_sensitive_data():
    """Test with sensitive data access enabled."""
    from well_architected_review_mcp_server_for_sagemaker.server import main

    with patch.object(argparse.ArgumentParser, 'parse_args') as mock_parse_args:
        mock_parse_args.return_value = argparse.Namespace(
            allow_sensitive_data_access=True, transport='stdio'
        )

        mock_server = MagicMock()
        with patch(
            'well_architected_review_mcp_server_for_sagemaker.server.create_server', return_value=mock_server
        ):
            with patch(
                'well_architected_review_mcp_server_for_sagemaker.server.WellArchitectedValidationHandler'
            ) as mock_handler:
                main()

                mock_handler.assert_called_once_with(mock_server, True)
                mock_server.run.assert_called_once_with(transport='stdio')


@pytest.mark.asyncio
async def test_command_line_args_streamable_http_transport():
    """Test that --transport streamable-http is passed through to create_server and run."""
    from well_architected_review_mcp_server_for_sagemaker.server import main

    with patch.object(argparse.ArgumentParser, 'parse_args') as mock_parse_args:
        mock_parse_args.return_value = argparse.Namespace(
            allow_sensitive_data_access=False, transport='streamable-http'
        )

        mock_server = MagicMock()
        with patch(
            'well_architected_review_mcp_server_for_sagemaker.server.create_server', return_value=mock_server
        ) as mock_create_server:
            with patch('well_architected_review_mcp_server_for_sagemaker.server.WellArchitectedValidationHandler'):
                main()

                mock_create_server.assert_called_once_with(transport='streamable-http')
                mock_server.run.assert_called_once_with(transport='streamable-http')


@pytest.mark.asyncio
async def test_handler_tool_registration():
    """Test that all tools are registered on the MCP instance."""
    mock_mcp = MagicMock()

    WellArchitectedValidationHandler(mock_mcp)

    assert mock_mcp.tool.call_count == 5

    tool_names = [call[1]['name'] for call in mock_mcp.tool.call_args_list]
    assert 'validate_sagemaker_resource' in tool_names
    assert 'validate_all_endpoints' in tool_names
    assert 'validate_all_resources' in tool_names
    assert 'list_sagemaker_resources' in tool_names
    assert 'get_pillar_details' in tool_names


@pytest.mark.asyncio
async def test_tool_result_is_not_double_wrapped():
    """Regression: tool results must be a single, un-nested CallToolResult.

    The bug: tools returned a custom model that shadowed mcp.types.CallToolResult,
    so FastMCP JSON-dumped the whole object into the result text — producing a
    tool response whose text contained another tool response (a 'content' key).
    """
    import json
    from well_architected_review_mcp_server_for_sagemaker.server import create_server
    from mcp.types import CallToolResult

    mcp = create_server()
    WellArchitectedValidationHandler(mcp)

    # get_pillar_details needs no AWS calls.
    result = await mcp.call_tool('get_pillar_details', {'pillar': 'security'})

    assert isinstance(result, CallToolResult)
    assert result.isError is False

    text = result.content[0].text
    parsed = json.loads(text)
    # Clean result: the text is the pillar payload itself, with no nested
    # 'content' envelope (the double-wrap signature).
    assert 'content' not in parsed
    assert parsed['name']

    # Structured data rides in structuredContent, not buried in the text.
    assert result.structuredContent is not None
    assert result.structuredContent['name'] == parsed['name']
