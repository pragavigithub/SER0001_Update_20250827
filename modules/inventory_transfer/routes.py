"""
Inventory Transfer Routes
All routes related to inventory transfers between warehouses/bins
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from app import db
from models import InventoryTransfer, InventoryTransferItem, User, SerialNumberTransfer, SerialNumberTransferItem, SerialNumberTransferSerial
from sqlalchemy import or_
import logging
import random
import re
import string
from datetime import datetime

transfer_bp = Blueprint('inventory_transfer', __name__, 
                         url_prefix='/inventory_transfer',
                         template_folder='templates')


@transfer_bp.route('/')
@login_required
def index():
    """Inventory Transfer main page - list all transfers for current user"""
    if not current_user.has_permission('inventory_transfer'):
        flash('Access denied - Inventory Transfer permissions required', 'error')
        return redirect(url_for('dashboard'))
    
    transfers = InventoryTransfer.query.filter_by(user_id=current_user.id).order_by(InventoryTransfer.created_at.desc()).all()
    return render_template('inventory_transfer.html', transfers=transfers)

@transfer_bp.route('/detail/<int:transfer_id>')
@login_required
def detail(transfer_id):
    """Inventory Transfer detail page"""
    transfer = InventoryTransfer.query.get_or_404(transfer_id)
    
    # Check permissions
    if transfer.user_id != current_user.id and current_user.role not in ['admin', 'manager', 'qc']:
        flash('Access denied - You can only view your own transfers', 'error')
        return redirect(url_for('inventory_transfer.index'))
    
    # Fetch SAP data for warehouse display and available items calculation
    sap_transfer_data = None
    available_items = []
    
    try:
        from sap_integration import SAPIntegration
        sap_b1 = SAPIntegration()
        
        # Always fetch SAP data to get available items (regardless of warehouse fields)
        logging.info(f"🔍 Fetching SAP data for transfer {transfer.transfer_request_number}")
        sap_transfer_data = sap_b1.get_inventory_transfer_request(transfer.transfer_request_number)
        
        logging.info(f"🔍 SAP response type: {type(sap_transfer_data)}")
        if sap_transfer_data:
            logging.info(f"🔍 SAP response keys: {sap_transfer_data.keys()}")
        
        if sap_transfer_data and 'StockTransferLines' in sap_transfer_data:
            lines = sap_transfer_data['StockTransferLines']
            logging.info(f"🔍 Found {len(lines)} stock transfer lines")
            
            # Calculate actual remaining quantities based on WMS transfers
            for sap_line in lines:
                item_code = sap_line.get('ItemCode')
                requested_qty = float(sap_line.get('Quantity', 0))
                
                logging.info(f"🔍 Processing line: {item_code} - Qty: {requested_qty}")
                
                # Calculate total transferred quantity for this item from WMS database
                transferred_qty = 0
                wms_item = InventoryTransferItem.query.filter_by(
                    inventory_transfer_id=transfer.id,
                    item_code=item_code
                ).first()
                
                if wms_item:
                    transferred_qty = float(wms_item.quantity or 0)
                    logging.info(f"🔍 WMS item found - transferred: {transferred_qty}")
                
                # Calculate remaining quantity
                remaining_qty = max(0, requested_qty - transferred_qty)
                
                # Determine actual line status based on remaining quantity
                actual_line_status = 'bost_Close' if remaining_qty <= 0 else 'bost_Open'
                
                # Create enhanced item data with calculated values
                enhanced_item = {
                    'ItemCode': item_code,
                    'ItemDescription': sap_line.get('ItemDescription', ''),
                    'Quantity': requested_qty,
                    'TransferredQuantity': transferred_qty,
                    'RemainingQuantity': remaining_qty,
                    'UnitOfMeasure': sap_line.get('UoMCode', sap_line.get('MeasureUnit', '')),
                    'FromWarehouseCode': sap_line.get('FromWarehouseCode'),
                    'ToWarehouseCode': sap_line.get('WarehouseCode'),
                    'LineStatus': actual_line_status  # Use calculated status
                }
                available_items.append(enhanced_item)
                logging.info(f"🔍 Added item to available_items: {item_code}")
                
            logging.info(f"✅ Calculated remaining quantities for {len(available_items)} available items")
            
            # Update warehouse data if missing from database
            if not transfer.from_warehouse or not transfer.to_warehouse:
                from_wh = sap_transfer_data.get('FromWarehouse')
                to_wh = sap_transfer_data.get('ToWarehouse')
                logging.info(f"✅ Fetched SAP warehouse data for display: From={from_wh}, To={to_wh}")
        else:
            logging.warning(f"❌ SAP returned no transfer data or lines. Data: {sap_transfer_data}")
            
    except Exception as e:
        logging.error(f"❌ Could not fetch SAP data: {e}")
        import traceback
        logging.error(traceback.format_exc())
    
    if not transfer.from_warehouse or not transfer.to_warehouse:
        logging.info(f"📋 Using database warehouse data: From={transfer.from_warehouse}, To={transfer.to_warehouse}")
    
    return render_template('inventory_transfer_detail.html', 
                         transfer=transfer, 
                         sap_transfer_data=sap_transfer_data,
                         available_items=available_items)

@transfer_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create():
    """Create new inventory transfer"""
    if not current_user.has_permission('inventory_transfer'):
        flash('Access denied - Inventory Transfer permissions required', 'error')
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        transfer_request_number = request.form.get('transfer_request_number')
        from_warehouse = request.form.get('from_warehouse')
        to_warehouse = request.form.get('to_warehouse')
        
        if not transfer_request_number:
            flash('Transfer request number is required', 'error')
            return redirect(url_for('inventory_transfer.create'))
        
        # Check if transfer already exists - but allow multiple transfers until SAP request is closed
        # We'll validate SAP status first, then decide if new transfer creation is allowed
        
        # Validate SAP B1 transfer request and fetch warehouse data
        sap_data = None
        try:
            from sap_integration import SAPIntegration
            sap_b1 = SAPIntegration()
            sap_data = sap_b1.get_inventory_transfer_request(transfer_request_number)
            
            if not sap_data:
                flash(f'Transfer request {transfer_request_number} not found in SAP B1', 'error')
                return redirect(url_for('inventory_transfer.create'))
            
            # Check if transfer request is open (available for processing)
            doc_status = sap_data.get('DocumentStatus') or sap_data.get('DocStatus', '')
            if doc_status != 'bost_Open':
                if doc_status == 'bost_Close':
                    flash(f'Transfer request {transfer_request_number} is closed and cannot be processed.', 'error')
                else:
                    flash(f'Transfer request {transfer_request_number} has invalid status ({doc_status}). Only open requests (bost_Open) can be processed.', 'error')
                return redirect(url_for('inventory_transfer.create'))
            
            # Allow multiple transfers to be created until SAP document status becomes "bost_Close"
            # No duplicate checking - multiple users can create transfers for the same request
            
            # Extract warehouse data from SAP
            from_warehouse = from_warehouse or sap_data.get('FromWarehouse')
            to_warehouse = to_warehouse or sap_data.get('ToWarehouse')
            
            logging.info(f"✅ SAP B1 validation passed - DocNum: {transfer_request_number}, Status: {doc_status}")
            logging.info(f"✅ Warehouses from SAP: From={from_warehouse}, To={to_warehouse}")
            
        except Exception as e:
            logging.warning(f"SAP B1 validation failed: {e}")
            flash(f'Could not validate transfer request in SAP B1: {str(e)}', 'error')
            return redirect(url_for('inventory_transfer.create'))

        # Create new transfer
        transfer = InventoryTransfer(
            transfer_request_number=transfer_request_number,
            user_id=current_user.id,
            from_warehouse=from_warehouse,
            to_warehouse=to_warehouse,
            status='draft'
        )
        
        db.session.add(transfer)
        db.session.commit()
        
        # Auto-populate items from SAP transfer request if available
        auto_populate = request.form.get('auto_populate_items') == 'on'
        if auto_populate and sap_data and 'StockTransferLines' in sap_data:
            try:
                lines = sap_data['StockTransferLines']
                # Only add open lines (not closed)
                open_lines = [line for line in lines if line.get('LineStatus', '') != 'bost_Close']
                
                for sap_line in open_lines:
                    # Create transfer item from SAP line with correct field mapping
                    item_code = sap_line.get('ItemCode', '')
                    quantity = float(sap_line.get('Quantity', 0))
                    
                    # Debug logging for quantities
                    logging.info(f"📦 Auto-populating item {item_code}: SAP Quantity={quantity}, LineStatus={sap_line.get('LineStatus', 'Unknown')}")
                    
                    transfer_item = InventoryTransferItem(
                        inventory_transfer_id=transfer.id,  # Fixed: use correct foreign key field
                        item_code=item_code,
                        item_name=sap_line.get('ItemDescription', ''),
                        quantity=quantity,
                        requested_quantity=quantity,  # Set requested quantity
                        transferred_quantity=0,  # Initially 0
                        remaining_quantity=quantity,  # Initially same as requested
                        unit_of_measure=sap_line.get('UoMCode', sap_line.get('MeasureUnit', '')),
                        from_bin='',  # Will be filled later
                        to_bin='',    # Will be filled later  
                        batch_number='',  # Will be filled later
                        qc_status='pending'
                    )
                    db.session.add(transfer_item)
                
                db.session.commit()
                logging.info(f"✅ Auto-populated {len(open_lines)} items from SAP transfer request {transfer_request_number}")
                flash(f'Inventory Transfer created with {len(open_lines)} auto-populated items from request {transfer_request_number}', 'success')
            except Exception as e:
                logging.error(f"Error auto-populating items: {e}")
                flash(f'Transfer created but could not auto-populate items: {str(e)}', 'warning')
        else:
            flash(f'Inventory Transfer created for request {transfer_request_number}', 'success')
        
        # Log status change
        log_status_change(transfer.id, None, 'draft', current_user.id, 'Transfer created')
        
        logging.info(f"✅ Inventory Transfer created for request {transfer_request_number} by user {current_user.username}")
        return redirect(url_for('inventory_transfer.detail', transfer_id=transfer.id))
    
    return render_template('inventory_transfer.html')

@transfer_bp.route('/<int:transfer_id>/submit', methods=['POST'])
@login_required
def submit(transfer_id):
    """Submit transfer for QC approval"""
    try:
        transfer = InventoryTransfer.query.get_or_404(transfer_id)
        
        # Check permissions
        if transfer.user_id != current_user.id:
            return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        if transfer.status != 'draft':
            return jsonify({'success': False, 'error': 'Only draft transfers can be submitted'}), 400
        
        if not transfer.items:
            return jsonify({'success': False, 'error': 'Cannot submit transfer without items'}), 400
        
        # Update status
        old_status = transfer.status
        transfer.status = 'submitted'
        transfer.updated_at = datetime.utcnow()
        db.session.commit()
        
        # Log status change
        log_status_change(transfer_id, old_status, 'submitted', current_user.id, 'Transfer submitted for QC approval')
        
        logging.info(f"📤 Inventory Transfer {transfer_id} submitted for QC approval")
        return jsonify({
            'success': True,
            'message': 'Transfer submitted for QC approval',
            'status': 'submitted'
        })
        
    except Exception as e:
        logging.error(f"Error submitting transfer: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@transfer_bp.route('/<int:transfer_id>/qc_approve', methods=['POST'])
@login_required
def qc_approve(transfer_id):
    """QC approve transfer and post to SAP B1"""
    try:
        transfer = InventoryTransfer.query.get_or_404(transfer_id)
        
        # Check QC permissions
        if not current_user.has_permission('qc_dashboard') and current_user.role not in ['admin', 'manager']:
            return jsonify({'success': False, 'error': 'QC permissions required'}), 403
        
        if transfer.status != 'submitted':
            return jsonify({'success': False, 'error': 'Only submitted transfers can be approved'}), 400
        
        # Get QC notes
        qc_notes = request.json.get('qc_notes', '') if request.is_json else request.form.get('qc_notes', '')
        
        # Mark items as approved
        for item in transfer.items:
            item.qc_status = 'approved'
        
        # Update transfer status
        old_status = transfer.status
        transfer.status = 'qc_approved'
        transfer.qc_approver_id = current_user.id
        transfer.qc_approved_at = datetime.utcnow()
        transfer.qc_notes = qc_notes
        
        # Post to SAP B1 as Stock Transfer - MUST succeed for approval
        from sap_integration import SAPIntegration
        sap = SAPIntegration()
        
        logging.info(f"🚀 Posting Inventory Transfer {transfer_id} to SAP B1...")
        sap_result = sap.post_inventory_transfer_to_sap(transfer)
        
        if not sap_result.get('success'):
            # Rollback approval if SAP posting fails
            db.session.rollback()
            sap_error = sap_result.get('error', 'Unknown SAP error')
            logging.error(f"❌ SAP B1 posting failed: {sap_error}")
            return jsonify({'success': False, 'error': f'SAP B1 posting failed: {sap_error}'}), 500
        
        # SAP posting succeeded - update with document number
        transfer.sap_document_number = sap_result.get('document_number')
        transfer.status = 'posted'
        logging.info(f"✅ Successfully posted to SAP B1: {transfer.sap_document_number}")
        
        db.session.commit()
        
        # Log status change
        log_status_change(transfer_id, old_status, 'posted', current_user.id, f'Transfer QC approved and posted to SAP B1 as {transfer.sap_document_number}')
        
        logging.info(f"✅ Inventory Transfer {transfer_id} QC approved and posted to SAP B1")
        return jsonify({
            'success': True,
            'message': f'Transfer QC approved and posted to SAP B1 as {transfer.sap_document_number}',
            'sap_document_number': transfer.sap_document_number
        })
        
    except Exception as e:
        logging.error(f"Error approving transfer: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@transfer_bp.route('/<int:transfer_id>/qc_reject', methods=['POST'])
@login_required
def qc_reject(transfer_id):
    """QC reject transfer"""
    try:
        transfer = InventoryTransfer.query.get_or_404(transfer_id)
        
        # Check QC permissions
        if not current_user.has_permission('qc_dashboard') and current_user.role not in ['admin', 'manager']:
            return jsonify({'success': False, 'error': 'QC permissions required'}), 403
        
        if transfer.status != 'submitted':
            return jsonify({'success': False, 'error': 'Only submitted transfers can be rejected'}), 400
        
        # Get rejection reason
        qc_notes = request.json.get('qc_notes', '') if request.is_json else request.form.get('qc_notes', '')
        
        if not qc_notes:
            return jsonify({'success': False, 'error': 'Rejection reason is required'}), 400
        
        # Mark items as rejected
        for item in transfer.items:
            item.qc_status = 'rejected'
        
        # Update transfer status
        old_status = transfer.status
        transfer.status = 'rejected'
        transfer.qc_approver_id = current_user.id
        transfer.qc_approved_at = datetime.utcnow()
        transfer.qc_notes = qc_notes
        
        db.session.commit()
        
        # Log status change
        log_status_change(transfer_id, old_status, 'rejected', current_user.id, f'Transfer rejected by QC: {qc_notes}')
        
        logging.info(f"❌ Inventory Transfer {transfer_id} rejected by QC")
        return jsonify({
            'success': True,
            'message': 'Transfer rejected by QC',
            'status': 'rejected'
        })
        
    except Exception as e:
        logging.error(f"Error rejecting transfer: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@transfer_bp.route('/<int:transfer_id>/reopen', methods=['POST'])
@login_required
def reopen(transfer_id):
    """Reopen a rejected transfer"""
    try:
        transfer = InventoryTransfer.query.get_or_404(transfer_id)
        
        # Check permissions
        if transfer.user_id != current_user.id and current_user.role not in ['admin', 'manager']:
            return jsonify({'success': False, 'error': 'Access denied - You can only reopen your own transfers'}), 403
        
        if transfer.status != 'rejected':
            return jsonify({'success': False, 'error': 'Only rejected transfers can be reopened'}), 400
        
        # Reset transfer to draft status
        old_status = transfer.status
        transfer.status = 'draft'
        transfer.qc_approver_id = None
        transfer.qc_approved_at = None
        transfer.qc_notes = None
        transfer.updated_at = datetime.utcnow()
        
        # Reset all items to pending
        for item in transfer.items:
            item.qc_status = 'pending'
        
        db.session.commit()
        
        # Log status change
        log_status_change(transfer_id, old_status, 'draft', current_user.id, 'Transfer reopened and reset to draft status')
        
        logging.info(f"🔄 Inventory Transfer {transfer_id} reopened and reset to draft status")
        return jsonify({
            'success': True,
            'message': 'Transfer reopened successfully. You can now edit and resubmit it.',
            'status': 'draft'
        })
        
    except Exception as e:
        logging.error(f"Error reopening transfer: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@transfer_bp.route('/<int:transfer_id>/add_item', methods=['POST'])
@login_required
def add_transfer_item(transfer_id):
    """Add item to inventory transfer with duplicate prevention"""
    try:
        transfer = InventoryTransfer.query.get_or_404(transfer_id)
        
        # Check permissions
        if transfer.user_id != current_user.id and current_user.role not in ['admin', 'manager']:
            flash('Access denied - You can only modify your own transfers', 'error')
            return redirect(url_for('inventory_transfer.detail', transfer_id=transfer_id))
        
        if transfer.status != 'draft':
            flash('Cannot add items to non-draft transfer', 'error')
            return redirect(url_for('inventory_transfer.detail', transfer_id=transfer_id))
        
        # Get form data
        item_code = request.form.get('item_code')
        item_name = request.form.get('item_name')
        quantity = float(request.form.get('quantity', 0))
        unit_of_measure = request.form.get('unit_of_measure')
        from_warehouse_code = request.form.get('from_warehouse_code')
        to_warehouse_code = request.form.get('to_warehouse_code')
        from_bin = request.form.get('from_bin')
        to_bin = request.form.get('to_bin')
        batch_number = request.form.get('batch_number')
        
        if not all([item_code, item_name, quantity > 0]):
            flash('Item Code, Item Name, and Quantity are required', 'error')
            return redirect(url_for('inventory_transfer.detail', transfer_id=transfer_id))
        
        # **DUPLICATE PREVENTION LOGIC FOR INVENTORY TRANSFERS**
        # Check if this item_code already exists in this transfer
        existing_item = InventoryTransferItem.query.filter_by(
            transfer_id=transfer_id,
            item_code=item_code
        ).first()
        
        if existing_item:
            flash(f'Item {item_code} has already been added to this inventory transfer. Each item can only be transferred once per transfer request to avoid duplication.', 'error')
            return redirect(url_for('inventory_transfer.detail', transfer_id=transfer_id))
        
        # Create new transfer item
        transfer_item = InventoryTransferItem(
            transfer_id=transfer_id,
            item_code=item_code,
            item_name=item_name,
            quantity=quantity,
            unit_of_measure=unit_of_measure,
            from_warehouse_code=from_warehouse_code,
            to_warehouse_code=to_warehouse_code,
            from_bin=from_bin,
            to_bin=to_bin,
            batch_number=batch_number,
            qc_status='pending'
        )
        
        db.session.add(transfer_item)
        db.session.commit()
        
        logging.info(f"✅ Item {item_code} added to inventory transfer {transfer_id} with duplicate prevention")
        flash(f'Item {item_code} successfully added to inventory transfer', 'success')
        
    except Exception as e:
        logging.error(f"Error adding item to inventory transfer: {str(e)}")
        flash(f'Error adding item: {str(e)}', 'error')
    
    return redirect(url_for('inventory_transfer.detail', transfer_id=transfer_id))

@transfer_bp.route('/items/<int:item_id>/delete', methods=['POST'])
@login_required
def delete_transfer_item(item_id):
    """Delete transfer item"""
    try:
        item = InventoryTransferItem.query.get_or_404(item_id)
        transfer = item.transfer
        
        # Check permissions
        if transfer.user_id != current_user.id and current_user.role not in ['admin', 'manager']:
            return jsonify({'success': False, 'error': 'Access denied'}), 403
        
        if transfer.status != 'draft':
            return jsonify({'success': False, 'error': 'Cannot delete items from non-draft transfer'}), 400
        
        transfer_id = transfer.id
        item_code = item.item_code
        
        db.session.delete(item)
        db.session.commit()
        
        logging.info(f"🗑️ Item {item_code} deleted from inventory transfer {transfer_id}")
        return jsonify({'success': True, 'message': f'Item {item_code} deleted'})
        
    except Exception as e:
        logging.error(f"Error deleting inventory transfer item: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

# Serial Number Transfer Routes
@transfer_bp.route('/serial')
@login_required
def serial_index():
    """Serial Number Transfer index page"""
    if not current_user.has_permission('inventory_transfer'):
        flash('Access denied - Inventory Transfer permissions required', 'error')
        return redirect(url_for('dashboard'))
    
    # Get pagination parameters
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    search = request.args.get('search', '')
    user_based = request.args.get('user_based', 'true')
    
    # Build query
    query = SerialNumberTransfer.query
    
    # Apply user-based filtering for non-admin users
    if current_user.role not in ['admin', 'manager'] or user_based == 'true':
        query = query.filter_by(user_id=current_user.id)
    
    # Apply search filter
    if search:
        search_filter = or_(
            SerialNumberTransfer.transfer_number.ilike(f'%{search}%'),
            SerialNumberTransfer.from_warehouse.ilike(f'%{search}%'),
            SerialNumberTransfer.to_warehouse.ilike(f'%{search}%'),
            SerialNumberTransfer.status.ilike(f'%{search}%')
        )
        query = query.filter(search_filter)
    
    # Apply ordering and pagination
    query = query.order_by(SerialNumberTransfer.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    transfers = pagination.items
    
    return render_template('serial_transfer_index.html', 
                         transfers=transfers, 
                         pagination=pagination,
                         search=search,
                         per_page=per_page,
                         user_based=user_based)


@transfer_bp.route('/serial/create')
@login_required
def serial_create():
    """Serial Number Transfer create page"""
    if not current_user.has_permission('inventory_transfer'):
        flash('Access denied - Inventory Transfer permissions required', 'error')
        return redirect(url_for('dashboard'))
    
    return render_template('serial_create_transfer.html')


@transfer_bp.route('/serial/<int:transfer_id>')
@login_required
def serial_detail(transfer_id):
    """Serial Number Transfer detail page"""
    transfer = SerialNumberTransfer.query.get_or_404(transfer_id)
    
    # Check permissions
    if transfer.user_id != current_user.id and current_user.role not in ['admin', 'manager', 'qc']:
        flash('Access denied - You can only view your own transfers', 'error')
        return redirect(url_for('inventory_transfer.serial_index'))
    
    return render_template('serial_transfer_detail.html', transfer=transfer)


def log_status_change(transfer_id, previous_status, new_status, changed_by_id, notes=None):
    """Log status change to history table"""
    try:
        # TODO: Add TransferStatusHistory model to main models.py if needed
        # history = TransferStatusHistory(
        #     transfer_id=transfer_id,
        #     previous_status=previous_status,
        #     new_status=new_status,
        #     changed_by_id=changed_by_id,
        #     notes=notes
        # )
        # db.session.add(history)
        # db.session.commit()
        logging.info(f"Status changed from {previous_status} to {new_status} by user {changed_by_id}")
    except Exception as e:
        logging.error(f"Error logging status change: {str(e)}")

