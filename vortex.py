import pandas as pd
import numpy as np
import asyncio
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
import ccxt.async_support as ccxt
import talib
from ta.volatility import BollingerBands as ta_BollingerBands
from ta.momentum import RSIIndicator as ta_RSIIndicator
from ta.momentum import StochasticOscillator as ta_StochasticOscillator
import warnings
warnings.filterwarnings('ignore')

# --- DEBUG AYARI ---
DEBUG_MODE = False

# --- API ve Telegram Ayarları ---
BINANCE_API_KEY = "enwRUrFkiJdbmisUS7SvUSywVMovQyPQBW3SDSeWHogDPhqYk52nNwyvGKihFZv5"
BINANCE_API_SECRET = "X3WF36a2MgIzzkVjZQeNDJyTeNGJcY9IrZWFen7WmO3FJf0fVnbvqyX8TkhQ0G8N"
TELEGRAM_BOT_TOKEN = "7840917598:AAHoVMA5rANTwSAp848vvxnLJEIVKInaP3o"
TELEGRAM_CHAT_ID = "5252131835"

# --- Bot Ayarları ---
TIMEFRAME_1H = "1h"
TIMEFRAME_4H = "4h"
TIMEFRAME_1D = "1d"
KLINES_LIMIT = 200
SCAN_INTERVAL_SECONDS = 60 * 15

# --- İndikatör Parametreleri ---
BB_PERIOD = 20
BB_STD_DEV = 2
RSI_PERIOD = 14
RSI_OVERSOLD_THRESHOLD_1H = 27
RSI_OVERSOLD_THRESHOLD_4H = 30
RSI_OVERSOLD_THRESHOLD_1D = 33
RSI_OVERBOUGHT_THRESHOLD_1H = 83
RSI_OVERBOUGHT_THRESHOLD_4H = 83
RSI_OVERBOUGHT_THRESHOLD_1D = 83
STOCHRSI_K_PERIOD = 14
STOCHRSI_FASTK_PERIOD = 3
STOCHRSI_FASTD_PERIOD = 3
ADX_PERIOD = 14
ADX_THRESHOLD = 25

# --- Sinyal Geçmişi ---
signal_history = {}

# --- Binance ve Telegram Başlatma ---
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY,
    'secret': BINANCE_API_SECRET,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'},
})
telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)

# --- Yardımcı Fonksiyonlar ---

async def fetch_spot_symbols():
    try:
        await exchange.load_markets()
        symbols = [symbol for symbol in exchange.markets.keys() 
                  if symbol.endswith('/USDT') and exchange.markets[symbol]['spot'] and exchange.markets[symbol]['active']]
        if DEBUG_MODE:
            print(f"DEBUG: Binance spot piyasasından {len(symbols)} USDT çifti bulundu.")
        return symbols
    except Exception as e:
        print(f"HATA: Spot sembolleri çekilirken hata: {e}")
        await send_telegram_message(f"⚠️ <b>HATA:</b> Spot sembolleri çekilirken hata: {e}")
        return []

async def send_telegram_message(message):
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')
        if DEBUG_MODE:
            print(f"DEBUG: Telegram'a mesaj gönderildi: {message[:50]}...")
        return True
    except TelegramError as e:
        print(f"HATA: Telegram mesajı gönderme hatası: {e}")
        return False
    except Exception as e:
        print(f"HATA: Bilinmeyen hata (Telegram): {e}")
        return False

async def get_klines_ccxt(symbol, timeframe, limit):
    retries = 3
    for i in range(retries):
        try:
            if symbol not in exchange.markets:
                print(f"UYARI: {symbol} sembolü Binance'te bulunamadı. Atlanıyor.")
                return pd.DataFrame()
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df[['open', 'high', 'low', 'close', 'volume']] = df[['open', 'high', 'low', 'close', 'volume']].apply(pd.to_numeric)
            return df
        except ccxt.NetworkError as e:
            print(f"UYARI: Ağ hatası ({symbol}) - {e}. {i+1}/{retries} deneme.")
            await asyncio.sleep(5 * (i + 1))
        except ccxt.ExchangeError as e:
            print(f"UYARI: Borsa hatası ({symbol}) - {e}.")
            return pd.DataFrame()
        except Exception as e:
            print(f"HATA: Genel hata ({symbol}): {e}")
            return pd.DataFrame()
    print(f"HATA: {symbol} için {retries} deneme sonrası veri çekilemedi.")
    return pd.DataFrame()

def calculate_stoch_rsi(df, rsi_period, k_period, d_period):
    required_len = rsi_period + k_period
    if len(df) < required_len:
        return pd.Series(index=df.index), pd.Series(index=df.index)
    rsi_series = talib.RSI(df['close'], timeperiod=rsi_period)
    if len(rsi_series.dropna()) < k_period:
        return pd.Series(index=df.index), pd.Series(index=df.index)
    lowest_rsi = rsi_series.rolling(window=k_period).min()
    highest_rsi = rsi_series.rolling(window=k_period).max()
    denominator = (highest_rsi - lowest_rsi)
    stoch_rsi_k = ((rsi_series - lowest_rsi) / denominator.replace(0, np.nan)) * 100
    stoch_rsi_k = stoch_rsi_k.fillna(method='ffill').fillna(0)
    stoch_rsi_d = stoch_rsi_k.rolling(window=d_period).mean()
    return stoch_rsi_k, stoch_rsi_d

def check_signal_history(symbol, timeframe, cooldown_minutes=60):
    current_time = datetime.now()
    key = (symbol, timeframe)
    if key in signal_history:
        last_signal_time = signal_history[key]
        time_diff = (current_time - last_signal_time).total_seconds() / 60
        if time_diff < cooldown_minutes:
            if DEBUG_MODE:
                print(f"DEBUG: {symbol} ({timeframe}) için sinyal {time_diff:.1f} dakika önce gönderildi. Atlanıyor.")
            return False
    signal_history[key] = current_time
    return True

# --- Strateji Fonksiyonları (AL) ---

def check_strategy_1_saatlik_al(df, symbol_name):
    required_data_points = max(BB_PERIOD, RSI_PERIOD, STOCHRSI_K_PERIOD + STOCHRSI_FASTD_PERIOD, ADX_PERIOD) + 1
    if len(df) < required_data_points:
        return False, ""
    
    upper_bb, middle_bb, lower_bb = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
    rsi_val = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    stoch_rsi_k, stoch_rsi_d = calculate_stoch_rsi(df, STOCHRSI_K_PERIOD, STOCHRSI_FASTK_PERIOD, STOCHRSI_FASTD_PERIOD)
    adx_val = talib.ADX(df['high'], df['low'], df['close'], timeperiod=ADX_PERIOD)
    
    if pd.isna(lower_bb.iloc[-1]) or pd.isna(rsi_val.iloc[-1]) or \
       pd.isna(stoch_rsi_k.iloc[-1]) or pd.isna(stoch_rsi_d.iloc[-1]) or pd.isna(adx_val.iloc[-1]):
        return False, ""
    
    current_close_price = df['close'].iloc[-1]
    is_bb_condition_met = current_close_price <= lower_bb.iloc[-1]
    is_rsi_condition_met = rsi_val.iloc[-1] <= RSI_OVERSOLD_THRESHOLD_1H
    is_stoch_rsi_condition_met = stoch_rsi_k.iloc[-1] >= stoch_rsi_d.iloc[-1]
    is_adx_condition_met = adx_val.iloc[-1] >= ADX_THRESHOLD
    
    confirmations = sum([is_bb_condition_met, is_rsi_condition_met, is_stoch_rsi_condition_met, is_adx_condition_met])
    
    if confirmations >= 4 and check_signal_history(symbol_name, TIMEFRAME_1H):
        signal_message = (f"🟢 <b>Boğa Atılımı: 1 SAATLİK AL</b>\n"
                         f"Coin: {symbol_name}\n"
                         f"Fiyat: {current_close_price:.4f} USDT\n"
                         f"Zaman: {datetime.now().strftime('%d-%m-%Y %H:%M')}")
        return True, signal_message
    return False, ""

def check_strategy_4_saatlik_al(df, symbol_name):
    required_data_points = max(BB_PERIOD, RSI_PERIOD, STOCHRSI_K_PERIOD + STOCHRSI_FASTD_PERIOD, ADX_PERIOD) + 1
    if len(df) < required_data_points:
        return False, ""
    
    upper_bb, middle_bb, lower_bb = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
    rsi_val = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    stoch_rsi_k, stoch_rsi_d = calculate_stoch_rsi(df, STOCHRSI_K_PERIOD, STOCHRSI_FASTK_PERIOD, STOCHRSI_FASTD_PERIOD)
    adx_val = talib.ADX(df['high'], df['low'], df['close'], timeperiod=ADX_PERIOD)
    
    if pd.isna(lower_bb.iloc[-1]) or pd.isna(rsi_val.iloc[-1]) or \
       pd.isna(stoch_rsi_k.iloc[-1]) or pd.isna(stoch_rsi_d.iloc[-1]) or pd.isna(adx_val.iloc[-1]):
        return False, ""
    
    current_close_price = df['close'].iloc[-1]
    is_bb_condition_met = current_close_price <= lower_bb.iloc[-1]
    is_rsi_condition_met = rsi_val.iloc[-1] <= RSI_OVERSOLD_THRESHOLD_4H
    is_stoch_rsi_condition_met = stoch_rsi_k.iloc[-1] >= stoch_rsi_d.iloc[-1]
    is_adx_condition_met = adx_val.iloc[-1] >= ADX_THRESHOLD
    
    confirmations = sum([is_bb_condition_met, is_rsi_condition_met, is_stoch_rsi_condition_met, is_adx_condition_met])
    
    if confirmations >= 4 and check_signal_history(symbol_name, TIMEFRAME_4H):
        signal_message = (f"🟢 <b>Boğa Atılımı: 4 SAATLİK AL</b>\n"
                         f"Coin: {symbol_name}\n"
                         f"Fiyat: {current_close_price:.4f} USDT\n"
                         f"Zaman: {datetime.now().strftime('%d-%m-%Y %H:%M')}")
        return True, signal_message
    return False, ""

def check_strategy_1_gunluk_al(df, symbol_name):
    required_data_points = max(BB_PERIOD, RSI_PERIOD, STOCHRSI_K_PERIOD + STOCHRSI_FASTD_PERIOD, ADX_PERIOD) + 1
    if len(df) < required_data_points:
        return False, ""
    
    upper_bb, middle_bb, lower_bb = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
    rsi_val = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    stoch_rsi_k, stoch_rsi_d = calculate_stoch_rsi(df, STOCHRSI_K_PERIOD, STOCHRSI_FASTK_PERIOD, STOCHRSI_FASTD_PERIOD)
    adx_val = talib.ADX(df['high'], df['low'], df['close'], timeperiod=ADX_PERIOD)
    
    if pd.isna(lower_bb.iloc[-1]) or pd.isna(rsi_val.iloc[-1]) or \
       pd.isna(stoch_rsi_k.iloc[-1]) or pd.isna(stoch_rsi_d.iloc[-1]) or pd.isna(adx_val.iloc[-1]):
        return False, ""
    
    current_close_price = df['close'].iloc[-1]
    is_bb_condition_met = current_close_price <= lower_bb.iloc[-1]
    is_rsi_condition_met = rsi_val.iloc[-1] <= RSI_OVERSOLD_THRESHOLD_1D
    is_stoch_rsi_condition_met = stoch_rsi_k.iloc[-1] >= stoch_rsi_d.iloc[-1]
    is_adx_condition_met = adx_val.iloc[-1] >= ADX_THRESHOLD
    
    confirmations = sum([is_bb_condition_met, is_rsi_condition_met, is_stoch_rsi_condition_met, is_adx_condition_met])
    
    if confirmations >= 4 and check_signal_history(symbol_name, TIMEFRAME_1D):
        signal_message = (f"🟢 <b>Boğa Atılımı: 1 GÜNLÜK AL</b>\n"
                         f"Coin: {symbol_name}\n"
                         f"Fiyat: {current_close_price:.4f} USDT\n"
                         f"Zaman: {datetime.now().strftime('%d-%m-%Y %H:%M')}")
        return True, signal_message
    return False, ""

# --- Strateji Fonksiyonları (SAT) ---

def check_strategy_1_saatlik_sat(df, symbol_name):
    required_data_points = max(BB_PERIOD, RSI_PERIOD, STOCHRSI_K_PERIOD + STOCHRSI_FASTD_PERIOD, ADX_PERIOD) + 1
    if len(df) < required_data_points:
        return False, ""
    
    upper_bb, middle_bb, lower_bb = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
    rsi_val = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    stoch_rsi_k, stoch_rsi_d = calculate_stoch_rsi(df, STOCHRSI_K_PERIOD, STOCHRSI_FASTK_PERIOD, STOCHRSI_FASTD_PERIOD)
    adx_val = talib.ADX(df['high'], df['low'], df['close'], timeperiod=ADX_PERIOD)
    
    if pd.isna(upper_bb.iloc[-1]) or pd.isna(rsi_val.iloc[-1]) or \
       pd.isna(stoch_rsi_k.iloc[-1]) or pd.isna(stoch_rsi_d.iloc[-1]) or pd.isna(adx_val.iloc[-1]):
        return False, ""
    
    current_close_price = df['close'].iloc[-1]
    is_bb_condition_met = current_close_price >= upper_bb.iloc[-1]
    is_rsi_condition_met = rsi_val.iloc[-1] >= RSI_OVERBOUGHT_THRESHOLD_1H
    is_stoch_rsi_condition_met = stoch_rsi_k.iloc[-1] <= stoch_rsi_d.iloc[-1]
    is_adx_condition_met = adx_val.iloc[-1] >= ADX_THRESHOLD
    is_red_candle = df['close'].iloc[-1] < df['open'].iloc[-1]
    is_rsi_decline = rsi_val.iloc[-1] < rsi_val.iloc[-2] if len(rsi_val) > 1 else False
    
    confirmations = sum([is_bb_condition_met, is_rsi_condition_met, is_stoch_rsi_condition_met, is_adx_condition_met, is_red_candle])
    
    if confirmations >= 5 and check_signal_history(symbol_name, TIMEFRAME_1H):
        signal_message = (f"🔴 <b>Ayı Dalgası: 1 SAATLİK SAT</b>\n"
                         f"Coin: {symbol_name}\n"
                         f"Fiyat: {current_close_price:.4f} USDT\n"
                         f"Zaman: {datetime.now().strftime('%d-%m-%Y %H:%M')}")
        return True, signal_message
    return False, ""

def check_strategy_4_saatlik_sat(df, symbol_name):
    required_data_points = max(BB_PERIOD, RSI_PERIOD, STOCHRSI_K_PERIOD + STOCHRSI_FASTD_PERIOD, ADX_PERIOD) + 1
    if len(df) < required_data_points:
        return False, ""
    
    upper_bb, middle_bb, lower_bb = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
    rsi_val = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    stoch_rsi_k, stoch_rsi_d = calculate_stoch_rsi(df, STOCHRSI_K_PERIOD, STOCHRSI_FASTK_PERIOD, STOCHRSI_FASTD_PERIOD)
    adx_val = talib.ADX(df['high'], df['low'], df['close'], timeperiod=ADX_PERIOD)
    
    if pd.isna(upper_bb.iloc[-1]) or pd.isna(rsi_val.iloc[-1]) or \
       pd.isna(stoch_rsi_k.iloc[-1]) or pd.isna(stoch_rsi_d.iloc[-1]) or pd.isna(adx_val.iloc[-1]):
        return False, ""
    
    current_close_price = df['close'].iloc[-1]
    is_bb_condition_met = current_close_price >= upper_bb.iloc[-1]
    is_rsi_condition_met = rsi_val.iloc[-1] >= RSI_OVERBOUGHT_THRESHOLD_4H
    is_stoch_rsi_condition_met = stoch_rsi_k.iloc[-1] <= stoch_rsi_d.iloc[-1]
    is_adx_condition_met = adx_val.iloc[-1] >= ADX_THRESHOLD
    is_red_candle = df['close'].iloc[-1] < df['open'].iloc[-1]
    is_rsi_decline = rsi_val.iloc[-1] < rsi_val.iloc[-2] if len(rsi_val) > 1 else False
    
    confirmations = sum([is_bb_condition_met, is_rsi_condition_met, is_stoch_rsi_condition_met, is_adx_condition_met, is_red_candle])
    
    if confirmations >= 5 and check_signal_history(symbol_name, TIMEFRAME_4H):
        signal_message = (f"🔴 <b>Ayı Dalgası: 4 SAATLİK SAT</b>\n"
                         f"Coin: {symbol_name}\n"
                         f"Fiyat: {current_close_price:.4f} USDT\n"
                         f"Zaman: {datetime.now().strftime('%d-%m-%Y %H:%M')}")
        return True, signal_message
    return False, ""

def check_strategy_1_gunluk_sat(df, symbol_name):
    required_data_points = max(BB_PERIOD, RSI_PERIOD, STOCHRSI_K_PERIOD + STOCHRSI_FASTD_PERIOD, ADX_PERIOD) + 1
    if len(df) < required_data_points:
        return False, ""
    
    upper_bb, middle_bb, lower_bb = talib.BBANDS(df['close'], timeperiod=BB_PERIOD, nbdevup=BB_STD_DEV, nbdevdn=BB_STD_DEV, matype=0)
    rsi_val = talib.RSI(df['close'], timeperiod=RSI_PERIOD)
    stoch_rsi_k, stoch_rsi_d = calculate_stoch_rsi(df, STOCHRSI_K_PERIOD, STOCHRSI_FASTK_PERIOD, STOCHRSI_FASTD_PERIOD)
    adx_val = talib.ADX(df['high'], df['low'], df['close'], timeperiod=ADX_PERIOD)
    
    if pd.isna(upper_bb.iloc[-1]) or pd.isna(rsi_val.iloc[-1]) or \
       pd.isna(stoch_rsi_k.iloc[-1]) or pd.isna(stoch_rsi_d.iloc[-1]) or pd.isna(adx_val.iloc[-1]):
        return False, ""
    
    current_close_price = df['close'].iloc[-1]
    is_bb_condition_met = current_close_price >= upper_bb.iloc[-1]
    is_rsi_condition_met = rsi_val.iloc[-1] >= RSI_OVERBOUGHT_THRESHOLD_1D
    is_stoch_rsi_condition_met = stoch_rsi_k.iloc[-1] <= stoch_rsi_d.iloc[-1]
    is_adx_condition_met = adx_val.iloc[-1] >= ADX_THRESHOLD
    is_red_candle = df['close'].iloc[-1] < df['open'].iloc[-1]
    is_rsi_decline = rsi_val.iloc[-1] < rsi_val.iloc[-2] if len(rsi_val) > 1 else False
    
    confirmations = sum([is_bb_condition_met, is_rsi_condition_met, is_stoch_rsi_condition_met, is_adx_condition_met, is_red_candle])
    
    if confirmations >= 5 and check_signal_history(symbol_name, TIMEFRAME_1D):
        signal_message = (f"🔴 <b>Ayı Dalgası: 1 GÜNLÜK SAT</b>\n"
                         f"Coin: {symbol_name}\n"
                         f"Fiyat: {current_close_price:.4f} USDT\n"
                         f"Zaman: {datetime.now().strftime('%d-%m-%Y %H:%M')}")
        return True, signal_message
    return False, ""

# --- Ana Tarama Döngüsü ---
async def main_scanner():
    print("Bot başlatıldı. Kripto piyasası taranıyor...")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("HATA: TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID gerekli.")
        return
    
    # Binance spot sembollerini çek
    SYMBOLS = await fetch_spot_symbols()
    if not SYMBOLS:
        print("HATA: Spot sembolleri alınamadı. Bot durduruluyor.")
        await send_telegram_message("⚠️ <b>HATA:</b> Binance spot sembolleri alınamadı.")
        return
    
    while True:
        await send_telegram_message(f"🔄 <b>Yeni Tarama Başladı:</b> {datetime.now().strftime('%d-%m-%Y %H:%M')} 🔄")
        print(f"\n--- Tarama Başlatıldı: {datetime.now().strftime('%d-%m-%Y %H:%M')} ---")
        
        active_signals_in_this_scan = []
        
        for symbol in SYMBOLS:
            await asyncio.sleep(exchange.rateLimit / 1000 / len(SYMBOLS) * 1.5)
            
            # 1 Saatlik (AL ve SAT)
            df_1h = await get_klines_ccxt(symbol, TIMEFRAME_1H, KLINES_LIMIT)
            if not df_1h.empty and len(df_1h) >= KLINES_LIMIT:
                is_signal_1_hour_buy, msg_1_hour_buy = check_strategy_1_saatlik_al(df_1h.copy(), symbol)
                if is_signal_1_hour_buy:
                    active_signals_in_this_scan.append(f"{symbol} (1 SAATLİK AL)")
                    await send_telegram_message(msg_1_hour_buy)
                is_signal_1_hour_sell, msg_1_hour_sell = check_strategy_1_saatlik_sat(df_1h.copy(), symbol)
                if is_signal_1_hour_sell:
                    active_signals_in_this_scan.append(f"{symbol} (1 SAATLİK SAT)")
                    await send_telegram_message(msg_1_hour_sell)
            
            # 4 Saatlik (AL ve SAT)
            df_4h = await get_klines_ccxt(symbol, TIMEFRAME_4H, KLINES_LIMIT)
            if not df_4h.empty and len(df_4h) >= KLINES_LIMIT:
                is_signal_4_hour_buy, msg_4_hour_buy = check_strategy_4_saatlik_al(df_4h.copy(), symbol)
                if is_signal_4_hour_buy:
                    active_signals_in_this_scan.append(f"{symbol} (4 SAATLİK AL)")
                    await send_telegram_message(msg_4_hour_buy)
                is_signal_4_hour_sell, msg_4_hour_sell = check_strategy_4_saatlik_sat(df_4h.copy(), symbol)
                if is_signal_4_hour_sell:
                    active_signals_in_this_scan.append(f"{symbol} (4 SAATLİK SAT)")
                    await send_telegram_message(msg_4_hour_sell)
            
            # 1 Günlük (AL ve SAT)
            df_1d = await get_klines_ccxt(symbol, TIMEFRAME_1D, KLINES_LIMIT)
            if not df_1d.empty and len(df_1d) >= KLINES_LIMIT:
                is_signal_1_day_buy, msg_1_day_buy = check_strategy_1_gunluk_al(df_1d.copy(), symbol)
                if is_signal_1_day_buy:
                    active_signals_in_this_scan.append(f"{symbol} (1 GÜNLÜK AL)")
                    await send_telegram_message(msg_1_day_buy)
                is_signal_1_day_sell, msg_1_day_sell = check_strategy_1_gunluk_sat(df_1d.copy(), symbol)
                if is_signal_1_day_sell:
                    active_signals_in_this_scan.append(f"{symbol} (1 GÜNLÜK SAT)")
                    await send_telegram_message(msg_1_day_sell)
        
        if not active_signals_in_this_scan:
            print("Bu taramada sinyal bulunamadı.")
            await send_telegram_message(f"ℹ️ <b>Tarama Sonucu:</b> Bu taramada sinyal bulunamadı.")
        else:
            print(f"\n--- Taramada Bulunan Sinyaller ({datetime.now().strftime('%H:%M')}) ---")
            for sig in active_signals_in_this_scan:
                print(sig)
        
        print(f"Taramalar tamamlandı. {SCAN_INTERVAL_SECONDS / 60} dakika bekleniyor...")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)
    
    await exchange.close()

# --- Ana Çalıştırma Bloğu ---
if __name__ == "__main__":
    try:
        asyncio.run(main_scanner())
    except KeyboardInterrupt:
        print("\nBot kullanıcı tarafından durduruldu.")
    except Exception as e:
        print(f"Bot çalışırken hata oluştu: {e}")
