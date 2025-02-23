from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.cloud import storage
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, 
    Filters, CallbackContext, CallbackQueryHandler
)

import uuid
import re
import os
from pathlib import Path

import time
import logging
from functools import wraps

import json  # You likely already have this imported


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot_timing.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = r"telegram-ocr-connection-7eb1d30dee09.json"



def timing_decorator(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        execution_time = end_time - start_time
        logger.info(f'{func.__name__} took {execution_time:.2f} seconds to execute')
        return result
    return wrapper


def TOCR():
    # Define scopes for Google APIs
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    root_bucket_name = 'andre_ocr_bot-bucket'

    @timing_decorator
    def download_from_gcs(bucket_name, object_name):
        start_time = time.time()
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        result = blob.download_as_string(), blob.download_as_text()
        logger.info(f'GCS download for {object_name} took {time.time() - start_time:.2f} seconds')
        return result

    def upload_to_gcs(bucket_name, object_name, data):
        """Uploads data to the specified object in the GCS bucket."""
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(data)

    # Get configuration from cloud storage
    _, config_texts = download_from_gcs(root_bucket_name, 'config.txt')
    texts = config_texts.split("\n")
    for text in texts:
        if 'telegram_bot_token' in text:
            TOKEN = text.split('=')[-1].strip()
        if 'google_gemini_api_key' in text:
            google_api_key = text.split('=')[-1].strip()

    # OCR function using Google Gemini
    import google.generativeai as genai

    @timing_decorator
    def do_ocr(image_content_or_path):
        """Extract text from bet slip images using Google Gemini API"""
        start_time = time.time()
        
        # Configure Gemini
        genai.configure(api_key=google_api_key)
        generation_config = {
            "temperature": 0.4,
            "top_p": 1,
            "top_k": 32
        }
        config_time = time.time()
        logger.info(f'Gemini configuration took {config_time - start_time:.2f} seconds')

        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
        ]

        # Model initialization
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            generation_config=generation_config,
            safety_settings=safety_settings
        )
        init_time = time.time()
        logger.info(f'Model initialization took {init_time - config_time:.2f} seconds')

        # Handle different input types and process image
        try:
            if isinstance(image_content_or_path, (str, Path)):
                image_bytes = Path(image_content_or_path).read_bytes()
            elif isinstance(image_content_or_path, bytes):
                image_bytes = image_content_or_path
            else:
                raise ValueError("Invalid input type. Expected file path or bytes.")
            
            image_process_time = time.time()
            logger.info(f'Image processing took {image_process_time - init_time:.2f} seconds')

            image_parts = [{"mime_type": "image/jpeg", "data": image_bytes}]

            # Modified prompt to better handle multiple teams with delimiters
            prompt_parts = [
                "Extract betting slip data in format:\n"
                "ID:\nDate:\nTime:\nCountry:\nMatch League:\nHome Team:\nAway Team:\n"
                "Staked Amount:\nPotential Winning:\nBet Option Staked:\n"
                "Odds of Bet Option Staked:\nTotal Odds:\nBet Status:\n"
                "##############\n"
                "Use semicolons for multiple teams/odds. Combined odds in Total Odds.",
                image_parts[0],
            ]

            prompt_time = time.time()
            logger.info(f'Prompt preparation took {prompt_time - image_process_time:.2f} seconds')

            # Generate content
            response = model.generate_content(prompt_parts)
            response.resolve()
            
            generation_time = time.time()
            logger.info(f'Content generation took {generation_time - prompt_time:.2f} seconds')
            
            return response.text
            
        except Exception as e:
            error_time = time.time()
            logger.error(f'Error in OCR processing after {error_time - start_time:.2f} seconds: {str(e)}')
            raise

    def generate_google_auth_url(user_id):
        """Generate authentication URL for Google OAuth"""
        # Get the redirect URL from environment or use default
        redirect_url = os.environ.get('REDIRECT_URL', 'https://web-production-acba3.up.railway.app/oauth-callback')
        
        # Generate state parameter with user_id
        state = str(user_id)
        
        # Return full auth URL with state
        return f"{redirect_url}?state={state}"
    
    def check_if_authenticated(user_id):
        """Check if a user has already authenticated with Google"""
        try:
            gcs_file_path = f"bot_user_tokens/{user_id}/token.json"
            storage_client = storage.Client()
            bucket = storage_client.bucket(root_bucket_name)
            blob = bucket.blob(gcs_file_path)
            return blob.exists()
        except Exception:
            return False
    
    @timing_decorator
    def do_gsheet_authentication(user_id):
        """Authenticate user access to Google Sheets"""
        start_time = time.time()
        creds = None
        gcs_file_path = f"bot_user_tokens/{user_id}/token.json"

        try:
            # Try to download tokens from GCS
            download_start = time.time()
            gcs_tokens, _ = download_from_gcs(root_bucket_name, gcs_file_path)
            download_time = time.time()
            logger.info(f'Token download took {download_time - download_start:.2f} seconds')
            
            # Process tokens
            toks_dict = json.loads(gcs_tokens)
            creds = Credentials.from_authorized_user_info(toks_dict)
            process_time = time.time()
            logger.info(f'Token processing took {process_time - download_time:.2f} seconds')
                
        except Exception as e:
            logger.error(f"Token retrieval error for user {user_id}: {e}")
            creds = None
                
        # Handle credential validation and refresh
        if not creds or not creds.valid:
            validation_start = time.time()
            if creds and creds.expired and creds.refresh_token:
                try:
                    # Try to refresh the token
                    logger.info(f"Attempting token refresh for user {user_id}")
                    refresh_start = time.time()
                    creds.refresh(Request())
                    logger.info(f"Token refresh took {time.time() - refresh_start:.2f} seconds")
                    
                    # Save the refreshed token
                    save_start = time.time()
                    upload_to_gcs(root_bucket_name, gcs_file_path, creds.to_json())
                    logger.info(f"Token save took {time.time() - save_start:.2f} seconds")
                    
                except Exception as refresh_error:
                    logger.error(f"Token refresh failed for user {user_id}: {refresh_error}")
                    creds = None
                    
                    # Handle invalid_grant error by deleting the token
                    if 'invalid_grant' in str(refresh_error):
                        try:
                            delete_start = time.time()
                            storage_client = storage.Client()
                            bucket = storage_client.bucket(root_bucket_name)
                            blob = bucket.blob(gcs_file_path)
                            if blob.exists():
                                blob.delete()
                                logger.info(f"Invalid token deletion took {time.time() - delete_start:.2f} seconds")
                        except Exception as delete_error:
                            logger.error(f"Error deleting invalid token: {delete_error}")
            
            logger.info(f'Credential validation and refresh took {time.time() - validation_start:.2f} seconds')

        total_time = time.time() - start_time
        logger.info(f'Total authentication process took {total_time:.2f} seconds')
        return creds
    
    @timing_decorator
    def do_values_extraction(text_from_ocr):
        """Extract bet values using simple regex patterns with support for both leg odds and total odds"""
        start_time = time.time()
        
        # Define regex patterns inside the function
        regex_patterns = {
            "ID": r"ID: (.+)",
            "Date": r"Date: (.+)",
            "Time": r"Time: (.+)",
            "Country": r"Country: (.+)",
            "Match League": r"Match League: (.+)",
            "Home Team": r"Home Team: (.+)",
            "Away Team": r"Away Team: (.+)",
            "Staked Amount": r"Staked Amount: (.+)",
            "Potential Winning": r"Potential Winning: (.+)",
            "Bet Option Staked": r"Bet Option Staked: (.+)",
            "Legs Odds": r"Odds of Bet Option Staked: (.+)",
            "Total Odds": r"Total Odds: (.+)",
            "Bet Status": r"Bet Status: (.+)"
        }

        extracted_values = {
            key: re.search(pattern, text_from_ocr).group(1) if re.search(pattern, text_from_ocr) else "NA"
            for key, pattern in regex_patterns.items()
        }

        # If ID wasn't provided, generate a random one
        if extracted_values["ID"] == "NA":
            extracted_values["ID"] = str(uuid.uuid4())[:8]
        
        # Special handling for Total Odds
        if extracted_values["Total Odds"] == "NA":
            # If no specific Total Odds but we have Legs Odds
            if extracted_values["Legs Odds"] != "NA":
                # Check if it's a multi-leg bet (contains semicolons)
                if ";" not in extracted_values["Legs Odds"]:
                    # For single leg, use the legs odds as total odds
                    extracted_values["Total Odds"] = extracted_values["Legs Odds"]

        logger.info(f'Value extraction took {time.time() - start_time:.2f} seconds')
        return extracted_values

    @timing_decorator
    def do_gsheet_update(user_id, text_to_write):
        """Update Google Sheet with extracted bet data using batch operations"""
        creds = do_gsheet_authentication(user_id)
        title = f"Track_record_{user_id}"

        try:
            service = build("sheets", "v4", credentials=creds)
            service_drive = build("drive", "v3", credentials=creds)

            # Get or create spreadsheet in a single operation
            spreadsheets = service_drive.files().list(
                q=f"name='{title}'",
                spaces='drive',
                fields='files(id, name)'
            ).execute()
            
            if spreadsheets.get('files'):
                spreadsheet_id = spreadsheets['files'][0]['id']
            else:
                spreadsheet = {"properties": {"title": title}}
                spreadsheet = service.spreadsheets().create(body=spreadsheet, fields="spreadsheetId").execute()
                spreadsheet_id = spreadsheet.get("spreadsheetId")

            # Extract values
            extracted_values = do_values_extraction(text_to_write)
            
            # Define header row
            header_values = [
                'ID', 'Date', 'Time', 'Country', 'League', 'Home', 'Away', 
                'Staked Amount', 'Potential Winning', 'Bet Option Staked', 
                'Legs Odds', 'Total Odds', 'Bet Status'
            ]
            
            # Prepare data row
            row_values = [extracted_values.get(key, 'NA') for key in [
                'ID', 'Date', 'Time', 'Country', 'Match League', 
                'Home Team', 'Away Team', 'Staked Amount', 'Potential Winning', 
                'Bet Option Staked', 'Legs Odds', 'Total Odds', 'Bet Status'
            ]]

            # Get sheet data in a single call
            range_name = "Sheet1!A1:M"
            sheet_data = service.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id,
                range=range_name
            ).execute()
            
            values = sheet_data.get('values', [])
            next_row = len(values) + 1

            # Prepare batch update
            batch_data = []
            
            # Add header if sheet is empty
            if not values:
                batch_data.append({
                    'range': 'Sheet1!A1:M1',
                    'values': [header_values]
                })
                next_row = 2

            # Add new row data
            batch_data.append({
                'range': f'Sheet1!A{next_row}:M{next_row}',
                'values': [row_values]
            })

            # Execute batch update
            body = {
                'valueInputOption': 'RAW',
                'data': batch_data
            }
            
            service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body=body
            ).execute()

            return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"

        except HttpError as error:
            logger.error(f"Sheet update error: {error}")
            return f"Error: {error}"
        
        
    def get_sheet_link(user_id):
        """Get the Google Sheet link for a user"""
        try:
            creds = do_gsheet_authentication(user_id)
            service_drive = build("drive", "v3", credentials=creds)
            title = f"Track_record_{user_id}"
            
            spreadsheets = service_drive.files().list().execute()
            existing_spreadsheet = next((s for s in spreadsheets.get("files", []) if s["name"] == title), None)
            
            if not existing_spreadsheet:
                return None
                
            spreadsheet_id = existing_spreadsheet["id"]
            return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        except Exception as e:
            print(f"Error getting sheet link: {e}")
            return None

    
    
    def reauth_command(update: Update, context: CallbackContext) -> None:
        """Force re-authentication with Google"""
        user_id = update.effective_user.id
        
        # Delete any existing token
        try:
            gcs_file_path = f"bot_user_tokens/{user_id}/token.json"
            storage_client = storage.Client()
            bucket = storage_client.bucket(root_bucket_name)
            blob = bucket.blob(gcs_file_path)
            if blob.exists():
                blob.delete()
        except Exception as e:
            print(f"Error deleting token during reauth: {e}")
        
        # Create auth button
        auth_url = generate_google_auth_url(user_id)
        keyboard = [
            [InlineKeyboardButton("Connect Google Account", url=auth_url)]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        update.message.reply_text(
            "To reconnect your Google account, please click the button below:",
            reply_markup=reply_markup
        )
        
    
    
    def handle_button(update: Update, context: CallbackContext):
        """Handle button callbacks"""
        query = update.callback_query
        query.answer()
        user_id = query.from_user.id
        data = query.data
        
        if data == 'upload':
            query.message.reply_text(
                "📸 Please send me a screenshot of your betting slip.\n"
                "💡 Tip: Screenshots from betting apps work best!"
            )
        
        elif data == 'view_sheet':
            sheet_link = get_sheet_link(user_id)
            if sheet_link:
                query.message.reply_text(
                    "📊 Your betting records:",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📑 Open Sheet", url=sheet_link)]
                    ])
                )
            else:
                query.message.reply_text("No sheet found. Please upload a betting slip first.")
        
        elif data == 'help':
            help_text = (
                "🔍 *How to use this bot:*\n\n"
                "1. Upload a photo of your betting slip\n"
                "2. I'll extract the information and save it\n"
                "3. View your betting history in Google Sheets\n\n"
                "*Tips for best results:*\n"
                "• Make sure the image is clear and well-lit\n"
                "• All text should be readable\n"
                "• Include all betting information in the image"
            )
            query.message.reply_text(help_text, parse_mode='Markdown')

    def sheet_command(update: Update, context: CallbackContext):
        """Handle /sheet command"""
        user_id = update.message.from_user.id
        sheet_link = get_sheet_link(user_id)
        if sheet_link:
            update.message.reply_text(
                "📊 Your betting records:",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📑 Open Sheet", url=sheet_link)]
                ])
            )
        else:
            update.message.reply_text("No sheet found. Please upload a betting slip first.")

    @timing_decorator
    def image_ocr(update: Update, context: CallbackContext):
        """Handle incoming images for OCR processing"""
        start_time = time.time()
        user_id = update.message.from_user.id
        
        # Check authentication
        auth_check_start = time.time()
        if not check_if_authenticated(user_id):
            auth_url = generate_google_auth_url(user_id)
            keyboard = [
                [InlineKeyboardButton("🔗 Connect Google Account", url=auth_url)]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            update.message.reply_text(
                "You need to connect your Google account first to process bet slips.",
                reply_markup=reply_markup
            )
            logger.info(f'Auth check failed and response sent in {time.time() - auth_check_start:.2f} seconds')
            return
        logger.info(f'Authentication check took {time.time() - auth_check_start:.2f} seconds')

        if update.message.photo or update.message.document:
            # Send processing message
            message_start = time.time()
            processing_msg = update.message.reply_text("🔄 Processing your betting slip...")
            logger.info(f'Processing message sent in {time.time() - message_start:.2f} seconds')

            try:
                # Get the file
                file_handling_start = time.time()
                if update.message.photo:
                    file_id = update.message.photo[-1].file_id
                else:  # document
                    file_id = update.message.document.file_id
                
                file_obj = context.bot.get_file(file_id)
                file_id_time = time.time()
                logger.info(f'File ID retrieval took {file_id_time - file_handling_start:.2f} seconds')
                
                # Download file
                file_path = file_obj.download()
                download_time = time.time()
                logger.info(f'File download took {download_time - file_id_time:.2f} seconds')

                # Perform OCR
                try:
                    ocr_start = time.time()
                    all_text = do_ocr(file_path)
                    ocr_time = time.time()
                    logger.info(f'OCR operation took {ocr_time - ocr_start:.2f} seconds')
                    
                    # Process OCR results
                    text_processing_start = time.time()
                    raw_text = all_text.split("##############\n")[0] if "##############\n" in all_text else ""
                    info_text = all_text.split("##############\n")[1] if "##############\n" in all_text else all_text
                    logger.info(f'Text splitting took {time.time() - text_processing_start:.2f} seconds')
                    
                    try:
                        # Update Google Sheet
                        sheet_start = time.time()
                        sheet_link = do_gsheet_update(user_id, info_text)
                        sheet_time = time.time()
                        logger.info(f'Sheet update took {sheet_time - sheet_start:.2f} seconds')
                        
                        # Handle sheet update response
                        if isinstance(sheet_link, str) and sheet_link.startswith("Error:"):
                            error_handling_start = time.time()
                            if "invalid_grant" in sheet_link:
                                # Token is invalid, prompt for reauth
                                auth_url = generate_google_auth_url(user_id)
                                keyboard = [
                                    [InlineKeyboardButton("🔄 Reconnect Google Account", url=auth_url)]
                                ]
                                reply_markup = InlineKeyboardMarkup(keyboard)
                                processing_msg.edit_text(
                                    "⚠️ Your Google authorization has expired.\n\n"
                                    "Please reconnect your account to continue:",
                                    reply_markup=reply_markup
                                )
                                
                                # Delete the invalid token
                                try:
                                    token_delete_start = time.time()
                                    gcs_file_path = f"bot_user_tokens/{user_id}/token.json"
                                    storage_client = storage.Client()
                                    bucket = storage_client.bucket(root_bucket_name)
                                    blob = bucket.blob(gcs_file_path)
                                    if blob.exists():
                                        blob.delete()
                                    logger.info(f'Token deletion took {time.time() - token_delete_start:.2f} seconds')
                                except Exception:
                                    logger.error("Failed to delete invalid token")
                                    pass
                            else:
                                processing_msg.edit_text(
                                    f"❌ Error during sheet update: {sheet_link}\n\n"
                                    "Please try again later."
                                )
                            logger.info(f'Error handling took {time.time() - error_handling_start:.2f} seconds')
                        else:
                            # Success response
                            response_start = time.time()
                            keyboard = [[InlineKeyboardButton("📑 View Sheet", url=sheet_link)]]
                            
                            processing_msg.edit_text(
                                "✅ Betting slip processed successfully!\n\n"
                                "📌 You can send more betting slips directly anytime!",
                                reply_markup=InlineKeyboardMarkup(keyboard)
                            )
                            logger.info(f'Success response sent in {time.time() - response_start:.2f} seconds')
                            
                    except Exception as e:
                        error_start = time.time()
                        error_message = str(e)
                        if "invalid_grant" in error_message:
                            invalidate_token_cache(user_id)
                            
                            # Token is invalid, prompt for reauth
                            auth_url = generate_google_auth_url(user_id)
                            keyboard = [
                                [InlineKeyboardButton("🔄 Reconnect Google Account", url=auth_url)]
                            ]
                            reply_markup = InlineKeyboardMarkup(keyboard)
                            processing_msg.edit_text(
                                "⚠️ Your Google authorization has expired.\n\n"
                                "Please reconnect your account to continue:",
                                reply_markup=reply_markup
                            )
                            
                            # Delete the invalid token
                            try:
                                token_delete_start = time.time()
                                gcs_file_path = f"bot_user_tokens/{user_id}/token.json"
                                storage_client = storage.Client()
                                bucket = storage_client.bucket(root_bucket_name)
                                blob = bucket.blob(gcs_file_path)
                                if blob.exists():
                                    blob.delete()
                                logger.info(f'Token deletion took {time.time() - token_delete_start:.2f} seconds')
                            except Exception:
                                logger.error("Failed to delete invalid token")
                                pass
                        else:
                            processing_msg.edit_text(
                                f"❌ Error updating sheet: {error_message}\n\n"
                                "Please try again later."
                            )
                        logger.info(f'Error handling took {time.time() - error_start:.2f} seconds')
                        
                except Exception as ocr_error:
                    ocr_error_start = time.time()
                    processing_msg.edit_text(
                        f"❌ Error during OCR processing: {str(ocr_error)}\n\n"
                        "Please try again with a clearer image."
                    )
                    logger.info(f'OCR error handling took {time.time() - ocr_error_start:.2f} seconds')
                    
            except Exception as file_error:
                file_error_start = time.time()
                processing_msg.edit_text(
                    f"❌ Error processing file: {str(file_error)}\n\n"
                    "Please try again with a different image format."
                )
                logger.info(f'File error handling took {time.time() - file_error_start:.2f} seconds')
                
            finally:
                # Cleanup
                cleanup_start = time.time()
                if 'file_path' in locals():
                    Path(file_path).unlink()
                logger.info(f'Cleanup took {time.time() - cleanup_start:.2f} seconds')
                
        else:
            update.message.reply_text("Please send me an image or document containing your betting slip.")
        
        # Log total processing time
        total_time = time.time() - start_time
        logger.info(f'Total image_ocr processing took {total_time:.2f} seconds')
            
            
            
    def start(update: Update, context: CallbackContext):
        """Handle /start command and authentication flow"""
        user_id = update.message.from_user.id
        # Check if user is already authenticated
        is_authenticated = check_if_authenticated(user_id)
        
        if not is_authenticated:
            # User needs to authenticate
            auth_url = generate_google_auth_url(user_id)
            welcome_text = (
                "👋 Welcome to Bet OCR Assistant!\n\n"
                "To get started, I need to connect to your Google account.\n"
                "This allows me to securely store your data in Google Sheets."
            )
            update.message.reply_text(
                welcome_text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Connect Google Account", url=auth_url)]
                ])
            )
        else:
            # User is already authenticated, show regular welcome
            welcome_text = (
                "👋 Welcome to Bet OCR Assistant!\n\n"
                "📸 Send me screenshots of your betting slips to track your bets.\n"
                "I'll extract the data and organize it in Google Sheets.\n\n"
                "💡 Tip: Clear, well-lit screenshots work best!"
            )
            # Create welcome buttons
            keyboard = [
                [
                    InlineKeyboardButton("📸 Upload Bet Slip", callback_data='upload'),
                    InlineKeyboardButton("📑 View Sheet", callback_data='view_sheet')
                ],
                [
                    InlineKeyboardButton("❔ Help", callback_data='help')
                ]
            ]
            update.message.reply_text(
                welcome_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    def help_command(update: Update, context: CallbackContext):
        """Handle the /help command"""
        help_text = (
            "📱 *Using Bet OCR Assistant:*\n\n"
            "• Send screenshots of your betting slips\n"
            "• Take screenshots directly from betting apps for best results\n"
            "• All information will be stored in your Google Sheet\n\n"
            "*Available commands:*\n"
            "/start - Start the bot\n"
            "/sheet - Access your Google Sheet\n"
            "/help - Show this help message"
        )
        update.message.reply_text(help_text, parse_mode='Markdown')

    def set_commands(updater):
        """Set up bot commands for menu"""
        commands = [
            BotCommand("start", "Start the bot"),
            BotCommand("help", "Get help using the bot"),
            BotCommand("sheet", "Get your Google Sheet link"),
            BotCommand("reauth", "Reconnect your Google account")
        ]
        updater.bot.set_my_commands(commands)

    def main():
        """Main function to run the bot"""
        updater = Updater(TOKEN)
        dp = updater.dispatcher

        # Set up commands
        set_commands(updater)

        # Add command handlers
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("help", help_command))
        dp.add_handler(CommandHandler("sheet", sheet_command))
        dp.add_handler(CommandHandler("reauth", reauth_command))
        
        # Add callback query handler
        dp.add_handler(CallbackQueryHandler(handle_button))
        
        # Add message handlers
        dp.add_handler(MessageHandler(Filters.photo | Filters.document, image_ocr))

        # Start the bot
        updater.start_polling()
        print("🤖 Bot is running...")
        updater.idle()

    if __name__ == '__main__':
        main()

print("\n[+] Bot is running...")
TOCR()