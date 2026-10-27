# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Action Plans Module - Manages deprecation remediation tracking
"""
import os
import uuid
import boto3
from boto3.dynamodb.conditions import Attr
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any

# Table name
ACTION_PLAN_TABLE = os.environ.get('ACTION_PLAN_TABLE_NAME', 'deprecation-action-plans')

def get_dynamodb_resource():
    """Get DynamoDB resource with region handling"""
    region = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION')
    if region:
        return boto3.resource('dynamodb', region_name=region)
    return boto3.resource('dynamodb')

def convert_decimals(obj: Any) -> Any:
    """Convert Decimal objects to int/float for JSON serialization"""
    if isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    elif isinstance(obj, dict):
        return {k: convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_decimals(i) for i in obj]
    return obj

# Index keys of the plans table (cdk/lib/data-stack.ts, terraform/data.tf):
# owner-index (owner, created_at) and plan-status-index (plan_status,
# target_date). DynamoDB refuses an empty string (or another type than a
# string) for an index key, so:
# - owner and plan_status must be non-empty strings;
# - a plan without a target date is stored WITHOUT the attribute (it is then
#   absent from plan-status-index) and is returned with target_date "" so the
#   UI keeps getting a string.
REQUIRED_INDEX_KEYS = ('owner', 'plan_status')


def _index_key_error(field: str, value: Any) -> Optional[str]:
    """Error message if value cannot be stored in this index key attribute, else None."""
    if field == 'target_date':
        if value in (None, '') or isinstance(value, str):
            return None
        return 'target_date must be a date string (YYYY-MM-DD) or empty'
    if not isinstance(value, str) or not value:
        return f'{field} must be a non-empty string'
    return None


def _for_response(plan: Dict) -> Dict:
    """A plan as the UI expects it: target_date always a string."""
    return {**plan, 'target_date': plan.get('target_date') or ''}


def _collect(call, **kwargs) -> List[Dict]:
    """All pages of a table scan or query."""
    items = []
    while True:
        response = call(**kwargs)
        items.extend(response.get('Items', []))
        if 'LastEvaluatedKey' not in response:
            return items
        kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']


def list_action_plans(filters: Optional[Dict] = None) -> Dict:
    """List all action plans with optional filtering"""
    try:
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(ACTION_PLAN_TABLE)
        
        # Check for specific filters
        if filters:
            owner = filters.get('owner')
            plan_status = filters.get('plan_status')
            
            if owner:
                # Query by owner using GSI
                plans = _collect(
                    table.query,
                    IndexName='owner-index',
                    KeyConditionExpression='#owner = :owner',
                    ExpressionAttributeNames={'#owner': 'owner'},
                    ExpressionAttributeValues={':owner': owner}
                )
            elif plan_status:
                # Not plan-status-index: plans without a target date are not in it
                plans = _collect(table.scan, FilterExpression=Attr('plan_status').eq(plan_status))
            else:
                plans = _collect(table.scan)
        else:
            plans = _collect(table.scan)
        
        plans = [_for_response(p) for p in plans]
        
        # Sort by created_at descending
        plans.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        return convert_decimals({
            'success': True,
            'plans': plans,
            'count': len(plans)
        })
        
    except Exception as e:
        return {'success': False, 'error': str(e)}

def get_action_plan(plan_id: str) -> Dict:
    """Get a specific action plan by ID"""
    try:
        if not plan_id:
            return {'success': False, 'error': 'plan_id is required'}
        
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(ACTION_PLAN_TABLE)
        
        response = table.get_item(Key={'plan_id': plan_id})
        
        if 'Item' not in response:
            return {'success': False, 'error': f'Action plan {plan_id} not found'}
        
        return convert_decimals({
            'success': True,
            'plan': _for_response(response['Item'])
        })
        
    except Exception as e:
        return {'success': False, 'error': str(e)}


def create_action_plan(data: Dict, caller: Optional[Dict] = None) -> Dict:
    """Create a new action plan.

    caller: the authenticated Cognito user of an HTTP request ({"sub", "name"}).
    When present it is recorded as created_by / created_by_sub and any
    created_by in the request body is ignored. A direct
    invocation (no caller) keeps the previous behaviour.
    """
    try:
        # Validate required fields
        required_fields = ['service_name', 'item_id', 'owner']
        for field in required_fields:
            if not data.get(field):
                return {'success': False, 'error': f'{field} is required'}
        
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(ACTION_PLAN_TABLE)
        
        now = datetime.now(timezone.utc).isoformat()
        plan_id = str(uuid.uuid4())
        
        plan = {
            'plan_id': plan_id,
            'service_name': data['service_name'],
            'item_id': data['item_id'],
            'item_name': data.get('item_name', data['item_id']),
            'owner': data['owner'],
            'plan_status': data.get('plan_status', 'not_started'),
            'priority': data.get('priority', 'medium'),
            'target_date': data.get('target_date'),
            'notes': data.get('notes', ''),
            'created_at': now,
            'updated_at': now,
            'created_by': caller['name'] if caller else data.get('created_by', data['owner']),
        }
        if caller:
            plan['created_by_sub'] = caller['sub']
        for field in REQUIRED_INDEX_KEYS + ('target_date',):
            error = _index_key_error(field, plan[field])
            if error:
                return {'success': False, 'error': error}
        if not plan['target_date']:
            del plan['target_date']  # no date: no index key attribute
        
        table.put_item(Item=plan)
        
        return convert_decimals({
            'success': True,
            'plan': _for_response(plan),
            'message': f'Action plan created with ID: {plan_id}'
        })
        
    except Exception as e:
        return {'success': False, 'error': str(e)}

def update_action_plan(plan_id: str, updates: Dict, caller: Optional[Dict] = None) -> Dict:
    """Update an existing action plan; records updated_by / updated_by_sub for an HTTP caller."""
    try:
        if not plan_id:
            return {'success': False, 'error': 'plan_id is required'}
        
        if not updates:
            return {'success': False, 'error': 'No updates provided'}
        
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(ACTION_PLAN_TABLE)
        
        # Check if plan exists
        existing = table.get_item(Key={'plan_id': plan_id})
        if 'Item' not in existing:
            return {'success': False, 'error': f'Action plan {plan_id} not found'}
        
        # Build update expression
        update_expr_parts = []
        remove_parts = []
        expr_attr_names = {}
        expr_attr_values = {}
        
        allowed_fields = ['owner', 'plan_status', 'priority', 'target_date', 'notes']
        
        for field in allowed_fields:
            if field in updates:
                if field in REQUIRED_INDEX_KEYS or field == 'target_date':
                    error = _index_key_error(field, updates[field])
                    if error:
                        return {'success': False, 'error': error}
                expr_attr_names[f'#{field}'] = field
                if field == 'target_date' and not updates[field]:
                    remove_parts.append(f'#{field}')  # date cleared: drop the index key attribute
                    continue
                update_expr_parts.append(f'#{field} = :{field}')
                expr_attr_values[f':{field}'] = updates[field]
        
        # Always update updated_at
        update_expr_parts.append('#updated_at = :updated_at')
        expr_attr_names['#updated_at'] = 'updated_at'
        expr_attr_values[':updated_at'] = datetime.now(timezone.utc).isoformat()
        if caller:
            for field, value in (('updated_by', caller['name']), ('updated_by_sub', caller['sub'])):
                update_expr_parts.append(f'#{field} = :{field}')
                expr_attr_names[f'#{field}'] = field
                expr_attr_values[f':{field}'] = value
        
        update_expr = 'SET ' + ', '.join(update_expr_parts)
        if remove_parts:
            update_expr += ' REMOVE ' + ', '.join(remove_parts)
        
        response = table.update_item(
            Key={'plan_id': plan_id},
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_attr_names,
            ExpressionAttributeValues=expr_attr_values,
            ReturnValues='ALL_NEW'
        )
        
        return convert_decimals({
            'success': True,
            'plan': _for_response(response.get('Attributes', {})),
            'message': f'Action plan {plan_id} updated'
        })
        
    except Exception as e:
        return {'success': False, 'error': str(e)}

def delete_action_plan(plan_id: str) -> Dict:
    """Delete an action plan"""
    try:
        if not plan_id:
            return {'success': False, 'error': 'plan_id is required'}
        
        dynamodb = get_dynamodb_resource()
        table = dynamodb.Table(ACTION_PLAN_TABLE)
        
        # Check if plan exists
        existing = table.get_item(Key={'plan_id': plan_id})
        if 'Item' not in existing:
            return {'success': False, 'error': f'Action plan {plan_id} not found'}
        
        table.delete_item(Key={'plan_id': plan_id})
        
        return {
            'success': True,
            'message': f'Action plan {plan_id} deleted'
        }
        
    except Exception as e:
        return {'success': False, 'error': str(e)}
