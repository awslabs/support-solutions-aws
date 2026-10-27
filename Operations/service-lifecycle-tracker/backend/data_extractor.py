# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Data Extractor for the Service Lifecycle Tracker on AWS
Combines HTML table parsing with LLM-guided extraction for high-quality data extraction
Handles the low-level extraction mechanics: HTML parsing, LLM processing, and data normalization
"""
import os
import requests
from bs4 import BeautifulSoup
import json
import re
import time
import logging
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError
from typing import Dict, List, Any, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from aws_utils import get_region
from database_reads import get_service_config
from page_check import keep_items_on_page
from service_filters import apply_service_filters

logger = logging.getLogger(__name__)

# -- Documentation fetch limits -------------------------
# Lifecycle facts come from the AWS documentation only: every URL, including
# each redirect target, must be https on one of these hosts.
ALLOWED_DOC_HOSTS = frozenset({'docs.aws.amazon.com'})
MAX_REDIRECTS = 5
# Decompressed page size cap. The configured pages are 15-130 KB today.
MAX_PAGE_BYTES = 5 * 1024 * 1024
FETCH_TIMEOUT_S = 30

class DocumentationFetchError(Exception):
    """A documentation URL was refused or could not be read within the limits."""


def check_documentation_url(url: str) -> str:
    """Return the URL if it may be fetched (https, allowed host, default port, no credentials)."""
    try:
        parts = urlsplit(str(url))
        port = parts.port
    except ValueError as e:
        raise DocumentationFetchError(f'Invalid documentation URL: {e}')
    if parts.scheme != 'https' or (parts.hostname or '').lower() not in ALLOWED_DOC_HOSTS:
        raise DocumentationFetchError(
            f"Refused URL {url!r}: only https://{' or '.join(sorted(ALLOWED_DOC_HOSTS))} pages are fetched")
    if parts.username or parts.password or port not in (None, 443):
        raise DocumentationFetchError(f'Refused URL {url!r}: credentials or a non-default port')
    return url

# Required fields that every Service_Config must contain
SERVICE_CONFIG_REQUIRED_FIELDS = [
    'name',
    'documentation_urls',
    'extraction_focus',
    'item_properties',
    'required_fields',
]

# Optional fields that are valid but not required
SERVICE_CONFIG_OPTIONAL_FIELDS = [
    'schema_key',
    'enabled',
    'last_extraction',
    'extraction_count',
]


def validate_service_config(config: dict) -> Tuple[bool, List[str]]:
    """
    Validate a Service_Config dictionary against the expected schema.

    Checks that all required fields are present and have the correct types.
    Optional fields (e.g. schema_key) are allowed without causing errors.

    Args:
        config: A service configuration dictionary to validate.

    Returns:
        A tuple of (is_valid, errors) where is_valid is True if the config passes
        all validation checks, and errors is a list of human-readable error messages
        describing any issues found.
    """
    errors: List[str] = []

    if not isinstance(config, dict):
        errors.append("Service config must be a dictionary")
        logger.warning("Invalid service config: not a dictionary")
        return False, errors

    # Check required fields are present
    for field in SERVICE_CONFIG_REQUIRED_FIELDS:
        if field not in config:
            errors.append(f"Missing required field: '{field}'")

    # Type validation for present fields
    if 'name' in config and not isinstance(config['name'], str):
        errors.append("Field 'name' must be a string")

    if 'documentation_urls' in config:
        if not isinstance(config['documentation_urls'], list):
            errors.append("Field 'documentation_urls' must be a list")
        elif len(config['documentation_urls']) == 0:
            errors.append("Field 'documentation_urls' must not be empty")
        else:
            for i, url in enumerate(config['documentation_urls']):
                if not isinstance(url, str):
                    errors.append(f"Field 'documentation_urls[{i}]' must be a string")

    if 'extraction_focus' in config and not isinstance(config['extraction_focus'], str):
        errors.append("Field 'extraction_focus' must be a string")

    if 'item_properties' in config and not isinstance(config['item_properties'], dict):
        errors.append("Field 'item_properties' must be a dictionary")

    if 'required_fields' in config:
        if not isinstance(config['required_fields'], list):
            errors.append("Field 'required_fields' must be a list")
        elif len(config['required_fields']) == 0:
            errors.append("Field 'required_fields' must not be empty")

    # Optional field type validation (only if present)
    if 'schema_key' in config and not isinstance(config['schema_key'], str):
        errors.append("Field 'schema_key' must be a string")

    if 'enabled' in config and not isinstance(config['enabled'], bool):
        errors.append("Field 'enabled' must be a boolean")

    if 'extraction_count' in config and not isinstance(config['extraction_count'], int):
        errors.append("Field 'extraction_count' must be an integer")

    is_valid = len(errors) == 0

    if not is_valid:
        config_name = config.get('name', '<unknown>')
        logger.warning(
            f"Invalid service config '{config_name}': {errors}"
        )

    return is_valid, errors


class DataExtractor:
    """
    Hybrid data extractor that combines HTML parsing with LLM processing
    
    Process:
    1. Fetch HTML documentation pages
    2. Parse HTML tables using BeautifulSoup
    3. Use LLM to extract and normalize deprecation data
    4. Return structured data ready for storage
    """
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Initialize Bedrock client using deployment region
        bedrock_region = get_region()
        self.bedrock = boto3.client('bedrock-runtime', region_name=bedrock_region)
    
    def extract_service_data(self, service_name: str, force_refresh: bool = False, override_urls: List[str] = None) -> Dict[str, Any]:
        """
        Extract deprecation data for a specific AWS service
        
        Args:
            service_name: AWS service identifier (e.g., 'lambda', 'eks')
            force_refresh: Whether to force fresh extraction (ignored for now)
            override_urls: Optional URLs to override service config URLs
            
        Returns:
            dict: Extraction results with success status, items, and processing details
        """
        
        # Start timing the extraction
        start_time = time.time()
        
        # Get service configuration
        config_result = get_service_config(service_name)
        if 'error' in config_result:
            return {
                'success': False,
                'service_name': service_name,
                'error': config_result['error']
            }
        
        service_config = config_result
        service_config['service_name'] = service_name  # Add service_name for filtering
        
        print(f"\n=== Extracting {service_config['name']} ===")
        
        # Use override URLs if provided, otherwise use config URLs
        urls_to_process = override_urls or service_config.get('documentation_urls', [])
        
        if not urls_to_process:
            return {
                'success': False,
                'service_name': service_name,
                'error': 'No documentation URLs configured for this service'
            }
        
        all_extracted_items = []
        processing_results = []
        
        # Process each documentation URL
        for url in urls_to_process:
            try:
                # Step 1: Fetch and parse HTML tables
                raw_data = self._fetch_html_tables(url)
                
                if raw_data['status'] != 'success':
                    processing_results.append({
                        "url": url,
                        "success": False,
                        "error": raw_data.get('error', 'Failed to fetch HTML tables')
                    })
                    continue
                
                # Step 2: Use LLM to extract and normalize deprecation data
                normalized_data = self._llm_extract_deprecation_data(
                    raw_data['extracted_data'], 
                    service_config
                )
                
                if 'error' in normalized_data:
                    processing_results.append({
                        "url": url,
                        "success": False,
                        "error": normalized_data['error']
                    })
                    continue
                
                # Keep only rows that appear in the page's tables:
                # anything else was invented by the model or planted by the page.
                items, dropped = keep_items_on_page(normalized_data.get('items', []), raw_data['table_text'])
                if dropped:
                    shown = [str(d.get('identifier') or d.get('name') or '?')[:60] if isinstance(d, dict) else '?'
                             for d in dropped[:10]]
                    print(f"NOT_ON_PAGE service={service_name} dropped={len(dropped)} kept={len(items)} rows={shown}")

                # Add source URL to each item
                for item in items:
                    item['source_url'] = url
                
                # Step 3: Apply service-specific filters
                filtered_items = apply_service_filters(service_name, items)
                
                all_extracted_items.extend(filtered_items)
                processing_results.append({
                    "url": url,
                    "success": True,
                    "items_extracted": len(filtered_items),
                    "items_before_filtering": len(items),
                    "items_not_on_page": len(dropped),
                    "tables_found": raw_data['tables_found']
                })
                
            except Exception as url_error:
                processing_results.append({
                    "url": url,
                    "success": False,
                    "error": str(url_error)
                })
        
        # Calculate extraction duration
        end_time = time.time()
        extraction_duration = round(end_time - start_time, 2)  # Round to 2 decimal places
        
        print(f"=== Extraction completed in {extraction_duration} seconds ===")
        
        return {
            'success': len(all_extracted_items) > 0,
            'service_name': service_name,
            'service_display_name': service_config['name'],
            'extraction_date': datetime.now(timezone.utc).isoformat(),
            'extraction_duration': extraction_duration,  # Add duration in seconds
            'total_items_extracted': len(all_extracted_items),
            'urls_processed': len(urls_to_process),
            'processing_results': processing_results,
            'items': all_extracted_items
        }
    
    def _fetch_html_tables(self, url: str) -> Dict[str, Any]:
        """
        Fetch HTML page and extract raw table data
        
        Args:
            url: Documentation URL to fetch
            
        Returns:
            dict: Status and extracted table data
        """
        
        print(f"Fetching: {url}")
        
        try:
            content = self._get_documentation_page(url)
            
            soup = BeautifulSoup(content, 'html.parser')
            
            # Find all table-contents divs (AWS docs structure)
            table_divs = soup.find_all('div', class_=['table-contents disable-scroll', 'table-contents'])
            
            print(f"Found {len(table_divs)} table-contents divs")
            
            extracted_data = []
            
            # Process table-contents divs
            for i, div in enumerate(table_divs):
                table_data = self._extract_table_data_from_div(div)
                if table_data:
                    extracted_data.append({
                        'type': 'table-contents-div',
                        'index': i + 1,
                        'data': table_data
                    })
            
            # If no table-contents divs found, try regular tables
            if not extracted_data:
                tables = soup.find_all('table')
                print(f"Found {len(tables)} regular tables")
                
                for i, table in enumerate(tables):
                    table_data = self._extract_table_data_from_table(table)
                    if table_data and table_data['rows']:
                        extracted_data.append({
                            'type': 'regular-table',
                            'index': i + 1,
                            'data': table_data
                        })
            
            # Every cell of the tables sent to the model: what model rows are checked against
            table_text = ' '.join(
                ' '.join(t['data']['headers']) + ' ' + ' '.join(' '.join(r) for r in t['data']['raw_rows'])
                for t in extracted_data)
            
            return {
                'status': 'success',
                'url': url,
                'tables_found': len(extracted_data),
                'extracted_data': extracted_data,
                'table_text': table_text,
            }
            
        except Exception as e:
            return {
                'status': 'error',
                'url': url,
                'error': str(e),
                'extracted_data': []
            }
    
    def _get_documentation_page(self, url: str) -> bytes:
        """GET a documentation page within the fetch limits above.

        Redirects are followed by hand so that every hop is checked against
        the allowed hosts; the body is read in chunks and refused past
        MAX_PAGE_BYTES (requests decompresses as it streams, so the cap
        applies to the decompressed size).
        """
        current = check_documentation_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            response = self.session.get(current, timeout=FETCH_TIMEOUT_S, allow_redirects=False, stream=True)
            try:
                if response.is_redirect:
                    current = check_documentation_url(urljoin(current, response.headers['Location']))
                    continue
                response.raise_for_status()
                body, size = [], 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > MAX_PAGE_BYTES:
                        raise DocumentationFetchError(f'{url} is larger than {MAX_PAGE_BYTES} bytes')
                    body.append(chunk)
                return b''.join(body)
            finally:
                response.close()
        raise DocumentationFetchError(f'{url}: more than {MAX_REDIRECTS} redirects')

    def _extract_table_data_from_div(self, div) -> Optional[Dict[str, Any]]:
        """Extract structured data from a table-contents div"""
        table = div.find('table')
        if not table:
            return None
        return self._extract_table_data_from_table(table)
    
    def _extract_table_data_from_table(self, table) -> Dict[str, Any]:
        """
        Extract structured data from a table element
        
        Args:
            table: BeautifulSoup table element
            
        Returns:
            dict: Structured table data with headers and rows
        """
        
        # Extract headers
        headers = []
        header_row = table.find('tr')
        if header_row:
            for th in header_row.find_all(['th', 'td']):
                headers.append(th.get_text(strip=True))
        
        # Extract data rows
        rows = []
        for tr in table.find_all('tr')[1:]:  # Skip header row
            row_data = []
            for td in tr.find_all(['td', 'th']):
                cell_text = td.get_text(strip=True)
                row_data.append(cell_text)
            
            if row_data:  # Only add non-empty rows
                rows.append(row_data)
        
        # Convert to structured format
        structured_data = []
        for row in rows:
            if len(row) >= len(headers):
                row_dict = {}
                for i, header in enumerate(headers):
                    if i < len(row):
                        row_dict[header] = row[i]
                structured_data.append(row_dict)
        
        return {
            'headers': headers,
            'rows': structured_data,
            'raw_rows': rows
        }
    
    def _llm_extract_deprecation_data(self, raw_tables: List[Dict], service_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Use LLM to extract and normalize deprecation data from raw tables
        
        Args:
            raw_tables: List of raw table data from HTML parsing
            service_config: Service configuration with extraction instructions
            
        Returns:
            dict: Normalized deprecation data or error information
        """
        
        # Prepare the prompt
        prompt = self._build_extraction_prompt(raw_tables, service_config)
        
        # Call LLM
        try:
            response = self._call_bedrock_llm(prompt)
            
            # Parse JSON response (handle markdown code blocks)
            try:
                # Remove markdown code blocks if present
                clean_response = response.strip()
                print(f"Raw response length: {len(clean_response)}")
                print(f"Response starts with: {repr(clean_response[:50])}")
                
                if clean_response.startswith('```json'):
                    clean_response = clean_response[7:]  # Remove ```json
                if clean_response.endswith('```'):
                    clean_response = clean_response[:-3]  # Remove ```
                clean_response = clean_response.strip()
                
                print(f"Clean response length: {len(clean_response)}")
                print(f"Clean response starts with: {repr(clean_response[:50])}")
                
                normalized_data = json.loads(clean_response)
                print(f"Successfully parsed JSON with {len(normalized_data.get('items', []))} items")
                
                return normalized_data
            except json.JSONDecodeError as e:
                print(f"JSON parsing error: {str(e)}")
                print(f"Error at position {e.pos if hasattr(e, 'pos') else 'unknown'}")
                print(f"Failed to parse around position: {repr(clean_response[max(0, e.pos-100):min(len(clean_response), e.pos+100)] if hasattr(e, 'pos') else 'N/A')}")
                print(f"Full response (first 2000 chars): {response[:2000]}")
                return {
                    'error': f'Failed to parse LLM response as JSON: {str(e)}',
                    'raw_response': response[:2000],  # Increased to 2000 chars for better debugging
                    'parse_error': str(e),
                    'error_position': e.pos if hasattr(e, 'pos') else None
                }
                
        except Exception as e:
            return {
                'error': f'LLM extraction failed: {str(e)}'
            }
    
    def _build_extraction_prompt(self, raw_tables: List[Dict], service_config: Dict[str, Any]) -> str:
        """
        Build the extraction prompt for the LLM
        
        Args:
            raw_tables: Raw table data from HTML parsing
            service_config: Service configuration with extraction instructions
            
        Returns:
            str: Complete prompt for LLM extraction
        """
        
        # Convert raw tables to text format
        tables_text = ""
        for i, table_info in enumerate(raw_tables):
            table_data = table_info['data']
            tables_text += f"\n=== Table {i+1} ({table_info['type']}) ===\n"
            tables_text += f"Headers: {table_data['headers']}\n"
            tables_text += "Rows:\n"
            for row in table_data['rows']:
                tables_text += f"  {row}\n"
        
        # Build the prompt using the successful pattern from our testing
        prompt = f"""Extract {service_config['name']} information from this table data.

{service_config['extraction_focus']}

TABLE DATA:
{tables_text}

Return JSON format:
{{
  "items": [
    {{
      {', '.join([f'"{key}": "{desc}"' for key, desc in service_config['item_properties'].items()])}
    }}
  ]
}}"""
        
        return prompt
    
    def _call_bedrock_llm(self, prompt: str) -> str:
        """
        Call Bedrock LLM with the extraction prompt
        
        Args:
            prompt: Complete extraction prompt
            
        Returns:
            str: LLM response text
        """
        
        model_id = "global.amazon.nova-2-lite-v1:0"
        
        # Prepare the request body for Nova Lite
        request_body = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": prompt}]
                }
            ],
            "inferenceConfig": {
                # issue #98, E4: 4000 tokens truncated the JSON for services with
                # large tables (e.g. ECS ~44 items), producing invalid JSON and
                # losing the whole batch. Nova 2 Lite supports up to 64K output
                # tokens; 16000 gives ample headroom for the largest tables while
                # staying well under the model limit.
                "maxTokens": 16000,
                "temperature": 0.1,
                "topP": 0.9
            }
        }
        
        try:
            response = self.bedrock.converse(
                modelId=model_id,
                **request_body
            )
            
            # Extract the response text
            response_text = response['output']['message']['content'][0]['text']
            return response_text
            
        except ClientError as e:
            print(f"Bedrock API error: {e}")
            raise
    
