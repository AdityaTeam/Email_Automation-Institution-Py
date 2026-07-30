// Bulk Email Sender - Main JavaScript

// Global variables
let recipients = [];
let templates = [];
let senders = [];

// Initialize on page load
document.addEventListener('DOMContentLoaded', function() {
    loadTemplates();
    loadSenders();
    setupEventListeners();
});

// Setup event listeners
function setupEventListeners() {
    // Template selection
    document.getElementById('templateSelect').addEventListener('change', handleTemplateChange);
    
    // File upload
    const uploadArea = document.getElementById('uploadArea');
    const fileInput = document.getElementById('fileInput');
    
    uploadArea.addEventListener('click', () => fileInput.click());
    
    uploadArea.addEventListener('dragover', (e) => {
        e.preventDefault();
        uploadArea.classList.add('dragover');
    });
    
    uploadArea.addEventListener('dragleave', () => {
        uploadArea.classList.remove('dragover');
    });
    
    uploadArea.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        if (e.dataTransfer.files.length) {
            handleFileUpload(e.dataTransfer.files[0]);
        }
    });
    
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length) {
            handleFileUpload(e.target.files[0]);
        }
    });
    
    // Preview button
    document.getElementById('previewBtn').addEventListener('click', showPreview);
    
    // Send button
    document.getElementById('sendBtn').addEventListener('click', sendEmails);
}

// Load email templates from API
async function loadTemplates() {
    try {
        const response = await fetch('/api/templates');
        const data = await response.json();
        
        if (data.templates) {
            templates = data.templates;
            const select = document.getElementById('templateSelect');
            select.innerHTML = '<option value="">-- Select Template --</option>';
            
            templates.forEach(template => {
                const option = document.createElement('option');
                option.value = template.id;
                option.textContent = template.name;
                option.dataset.subject = template.subject;
                option.dataset.body = template.body;
                select.appendChild(option);
            });
        }
    } catch (error) {
        console.error('Error loading templates:', error);
    }
}

// Load sender emails from API
async function loadSenders() {
    try {
        const response = await fetch('/api/senders');
        const data = await response.json();
        
        if (data.senders) {
            senders = data.senders;
            const select = document.getElementById('senderSelect');
            select.innerHTML = '<option value="">-- Select Sender --</option>';
            
            senders.forEach(sender => {
                const option = document.createElement('option');
                option.value = sender.email;
                option.textContent = sender.email;
                select.appendChild(option);
            });
            
            // Set default sender
            if (data.default) {
                select.value = data.default;
            }
        }
    } catch (error) {
        console.error('Error loading senders:', error);
    }
}

// Handle template selection
function handleTemplateChange() {
    const select = document.getElementById('templateSelect');
    const selectedOption = select.options[select.selectedIndex];
    
    if (selectedOption && selectedOption.value) {
        const subject = selectedOption.dataset.subject;
        const body = selectedOption.dataset.body;
        
        document.getElementById('emailSubject').value = subject;
        document.getElementById('emailBody').value = body;
    }
}

// Handle file upload
async function handleFileUpload(file) {
    const formData = new FormData();
    formData.append('file', file);
    
    try {
        const response = await fetch('/api/upload', {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (data.success) {
            recipients = data.recipients;
            displayRecipients();
            showAlert('success', `Loaded ${data.count} recipients successfully!`);
        } else {
            showAlert('danger', data.error || 'Error uploading file');
        }
    } catch (error) {
        console.error('Error uploading file:', error);
        showAlert('danger', 'Error uploading file. Please try again.');
    }
}

// Display recipients in table
function displayRecipients() {
    const countEl = document.getElementById('recipientCount');
    const noRecipientsEl = document.getElementById('noRecipients');
    const tableEl = document.getElementById('recipientTable');
    const tbodyEl = document.getElementById('recipientTableBody');
    
    countEl.textContent = recipients.length;
    
    if (recipients.length === 0) {
        noRecipientsEl.style.display = 'block';
        tableEl.style.display = 'none';
        return;
    }
    
    noRecipientsEl.style.display = 'none';
    tableEl.style.display = 'table';
    
    tbodyEl.innerHTML = '';
    
    recipients.forEach((r, index) => {
        const row = document.createElement('tr');
        row.innerHTML = `
            <td>${index + 1}</td>
            <td>${r.email}</td>
            <td>${r.name || '-'}</td>
            <td>${r.institute || '-'}</td>
        `;
        tbodyEl.appendChild(row);
    });
}

// Show preview modal
function showPreview() {
    const subject = document.getElementById('emailSubject').value;
    const body = document.getElementById('emailBody').value;
    const isHtml = document.getElementById('htmlFormat').checked;
    
    if (!subject || !body) {
        showAlert('warning', 'Please enter subject and body');
        return;
    }
    
    // Create preview with sample personalization
    let previewSubject = subject.replace('{{name}}', 'John Doe');
    previewSubject = previewSubject.replace('{{institute}}', 'Sample Institute');
    
    let previewBody = body.replace('{{name}}', 'John Doe');
    previewBody = previewBody.replace('{{institute}}', 'Sample Institute');
    
    document.getElementById('previewSubject').textContent = previewSubject;
    document.getElementById('previewBody').textContent = previewBody;
    
    const modal = new bootstrap.Modal(document.getElementById('previewModal'));
    modal.show();
}

// Send emails
async function sendEmails() {
    const senderEmail = document.getElementById('senderSelect').value;
    const subject = document.getElementById('emailSubject').value;
    const body = document.getElementById('emailBody').value;
    const isHtml = document.getElementById('htmlFormat').checked;
    
    // Validation
    if (!senderEmail) {
        showAlert('warning', 'Please select a sender email');
        return;
    }
    
    if (!subject || !body) {
        showAlert('warning', 'Please enter subject and body');
        return;
    }
    
    if (recipients.length === 0) {
        showAlert('warning', 'Please upload recipients first');
        return;
    }
    
    // Show loading overlay
    document.getElementById('loadingOverlay').classList.add('active');
    
    // Show progress card
    document.getElementById('progressCard').classList.add('active');
    
    // Reset counters
    document.getElementById('sentCount').textContent = '0';
    document.getElementById('failedCount').textContent = '0';
    document.getElementById('totalCount').textContent = recipients.length;
    document.getElementById('progressBar').style.width = '0%';
    document.getElementById('progressBar').textContent = '0%';
    
    try {
        const response = await fetch('/api/send', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                sender_email: senderEmail,
                subject: subject,
                body: body,
                is_html: isHtml,
                recipients: recipients
            })
        });
        
        const data = await response.json();
        
        // Hide loading overlay
        document.getElementById('loadingOverlay').classList.remove('active');
        
        if (data.success) {
            // Update progress
            document.getElementById('sentCount').textContent = data.sent;
            document.getElementById('failedCount').textContent = data.failed;
            document.getElementById('progressBar').style.width = '100%';
            document.getElementById('progressBar').textContent = '100%';
            
            // Show result modal
            document.getElementById('resultMessage').textContent = 
                `Successfully sent ${data.sent} emails. ${data.failed} failed.`;
            const resultModal = new bootstrap.Modal(document.getElementById('resultModal'));
            resultModal.show();
        } else {
            showAlert('danger', data.error || 'Error sending emails');
        }
        
    } catch (error) {
        console.error('Error sending emails:', error);
        document.getElementById('loadingOverlay').classList.remove('active');
        showAlert('danger', 'Error sending emails. Please try again.');
    }
}

// Show alert message
function showAlert(type, message) {
    const alertDiv = document.createElement('div');
    alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
    alertDiv.setAttribute('role', 'alert');
    alertDiv.innerHTML = `
        ${message}
        <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
    `;
    
    // Insert after the h2 heading
    const mainContent = document.querySelector('.main-content');
    mainContent.insertBefore(alertDiv, mainContent.children[1]);
    
    // Auto dismiss after 5 seconds
    setTimeout(() => {
        alertDiv.remove();
    }, 5000);
}
