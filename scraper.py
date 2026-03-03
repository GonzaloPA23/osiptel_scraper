# -*- coding: utf-8 -*-
import os, re, time, random, subprocess
from collections import Counter
from typing import Dict
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoSuchElementException

IS_LINUX = os.name != "nt"
URL = "https://checatuslineas.osiptel.gob.pe/"
TIMEOUT = 40

app = FastAPI(title="OSIPTEL Scraper API", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

class ConsultaRequest(BaseModel):
    ruc: str
    @field_validator("ruc")
    @classmethod
    def validar_ruc(cls, v):
        ruc_limpio = re.sub(r"\D", "", v)
        if len(ruc_limpio) != 11:
            raise ValueError("El RUC debe tener exactamente 11 digitos")
        return ruc_limpio

class ConsultaResponse(BaseModel):
    ruc: str
    estado: str
    mensaje: str
    q_entel: int
    q_claro: int
    q_movistar: int
    q_bitel: int
    q_wom: int
    q_otros: int
    q_total: int

class DriverManager:
    def __init__(self):
        self.driver = None

    def _safe_quit(self):
        try:
            if self.driver:
                self.driver.quit()
        except Exception:
            pass
        if not IS_LINUX:
            for proc in ["chromedriver.exe", "chrome.exe"]:
                subprocess.run(["taskkill", "/F", "/IM", proc, "/T"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        self.driver = None

    def build(self):
        self._safe_quit()
        opts = Options()
        if IS_LINUX:
            opts.add_argument("--headless=new")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--window-size=1920,1080")
            opts.add_argument("--disable-setuid-sandbox")
            opts.add_argument("--disable-extensions")
            opts.add_argument("--disable-software-rasterizer")
            opts.binary_location = "/usr/bin/google-chrome"
        else:
            opts.add_argument("--start-maximized")
            opts.add_argument("--disable-gpu")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--lang=es-PE")
        opts.add_experimental_option("excludeSwitches", ["enable-logging", "enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.page_load_strategy = "normal"
        for intento in range(5):
            try:
                print(f"Chrome intento {intento+1} | Linux={IS_LINUX}")
                if IS_LINUX:
                    service = Service("/usr/bin/chromedriver")
                else:
                    from webdriver_manager.chrome import ChromeDriverManager
                    service = Service(ChromeDriverManager().install())
                drv = webdriver.Chrome(service=service, options=opts)
                drv.set_page_load_timeout(60)
                drv.implicitly_wait(3)
                drv.execute_script("return navigator.userAgent;")
                self.driver = drv
                print(f"Chrome OK | Session: {drv.session_id}")
                return drv
            except Exception as e:
                print(f"Error Chrome intento {intento+1}: {e}")
                self._safe_quit()
                time.sleep(5)
        raise RuntimeError("No se pudo iniciar Chrome")

    def get(self):
        if self.driver is None:
            self.build()
        try:
            self.driver.execute_script("return 1;")
            return self.driver
        except Exception:
            self.build()
            return self.driver

    def go_home(self) -> bool:
        drv = self.get()
        for intento in range(6):
            try:
                print(f"Cargando OSIPTEL intento {intento+1}...")
                drv.get(URL)
                WebDriverWait(drv, TIMEOUT).until(
                    EC.presence_of_element_located((By.ID, "IdTipoDoc"))
                )
                print("OSIPTEL cargado OK.")
                time.sleep(1)
                return True
            except Exception as e:
                print(f"Fallo OSIPTEL intento {intento+1}: {type(e).__name__}: {str(e)[:200]}")
                if intento >= 2:
                    try:
                        self.build()
                        drv = self.driver
                    except Exception as be:
                        print(f"Error reconstruyendo Chrome: {be}")
                time.sleep(random.uniform(4, 7))
        print("No se pudo cargar OSIPTEL tras 6 intentos")
        return False

    def reiniciar(self):
        self.build()
        return self.go_home()

driver_mgr = DriverManager()

def normalize_operator(op: str) -> str:
    op = op.upper()
    if "ENTEL" in op: return "ENTEL"
    if "AMERICA MOVIL" in op or "CLARO" in op: return "CLARO"
    if "MOVISTAR" in op or "TELEFONICA" in op: return "MOVISTAR"
    if "BITEL" in op or "VIETTEL" in op: return "BITEL"
    if "WOM" in op: return "WOM"
    return "OTROS"

def esta_en_osiptel(drv) -> bool:
    try:
        drv.find_element(By.ID, "IdTipoDoc")
        return True
    except Exception:
        return False

def esperar_tabla(driver, max_espera=30) -> bool:
    start = time.time()
    while time.time() - start < max_espera:
        if "La consulta no se pudo procesar" in driver.page_source:
            return False
        try:
            spinner = driver.find_element(By.XPATH, "//*[contains(text(), 'Procesando')]")
            if spinner.is_displayed():
                time.sleep(0.5)
                continue
        except Exception:
            pass
        rows = driver.find_elements(By.CSS_SELECTOR, "#GridConsulta tbody tr")
        if rows and any(r.text.strip() for r in rows):
            return True
        if "No se encontraron resultados" in driver.page_source:
            return False
        time.sleep(0.5)
    return False

def collect_counts(driver) -> Counter:
    counts = Counter()
    rows = driver.find_elements(By.CSS_SELECTOR, "#GridConsulta tbody tr")
    for r in rows:
        tds = r.find_elements(By.TAG_NAME, "td")
        if len(tds) >= 3:
            counts[normalize_operator(tds[-1].text)] += 1
    return counts

def paginate_all(driver) -> Dict[str, int]:
    counts = Counter()
    try:
        Select(driver.find_element(By.NAME, "GridConsulta_length")).select_by_value("100")
        time.sleep(random.uniform(2, 3))
    except Exception:
        pass
    page = 1
    while True:
        esperar_tabla(driver, 10)
        counts.update(collect_counts(driver))
        print(f"Pagina {page} | total: {sum(counts.values())}")
        page += 1
        try:
            next_btn = driver.find_element(By.ID, "GridConsulta_next")
            if "disabled" in (next_btn.get_attribute("class") or "").lower():
                break
            driver.execute_script("arguments[0].click();", next_btn.find_element(By.TAG_NAME, "a"))
            time.sleep(random.uniform(1.5, 2))
        except NoSuchElementException:
            break
    return dict(counts)

def scrape_ruc(ruc: str) -> Dict:
    for intento in range(1, 4):
        try:
            drv = driver_mgr.get()
            print(f"[{ruc}] Intento {intento}...")
            if not esta_en_osiptel(drv):
                print("OSIPTEL no cargado, navegando...")
                ok = driver_mgr.go_home()
                if not ok:
                    if intento < 3:
                        continue
                    return {"estado": "ERROR", "mensaje": "No se pudo cargar OSIPTEL", "counts": {}}
                drv = driver_mgr.get()
            Select(drv.find_element(By.ID, "IdTipoDoc")).select_by_value("2")
            box = drv.find_element(By.ID, "NumeroDocumento")
            box.clear()
            box.send_keys(ruc)
            drv.find_element(By.ID, "btnBuscar").click()
            time.sleep(random.uniform(2, 3))
            if not esperar_tabla(drv, 25):
                if intento < 3:
                    driver_mgr.go_home()
                    continue
                return {"estado": "VACIO", "mensaje": "Sin resultados", "counts": {}}
            counts = paginate_all(drv)
            total = sum(counts.values())
            return {
                "estado": "OK" if total > 0 else "VACIO",
                "mensaje": f"{total} lineas encontradas" if total > 0 else "Sin resultados",
                "counts": counts
            }
        except Exception as e:
            print(f"Error intento {intento}: {type(e).__name__}: {str(e)[:150]}")
            if intento < 3:
                driver_mgr.reiniciar()
            else:
                return {"estado": "ERROR", "mensaje": str(e)[:120], "counts": {}}
    return {"estado": "ERROR", "mensaje": "Fallo tras 3 intentos", "counts": {}}

@app.on_event("startup")
async def startup():
    print("API OSIPTEL v2 lista. Llama /warmup para inicializar Chrome.")

@app.on_event("shutdown")
async def shutdown():
    driver_mgr._safe_quit()

@app.get("/")
def root():
    return {"mensaje": "API OSIPTEL activa", "version": "2.0.0"}

@app.get("/health")
def health():
    try:
        driver_mgr.get().execute_script("return 1;")
        chrome_ok = True
    except Exception:
        chrome_ok = False
    return {"api": "ok", "chrome": "ok" if chrome_ok else "caido"}

@app.get("/warmup")
def warmup():
    try:
        driver_mgr.build()
        ok = driver_mgr.go_home()
        if ok:
            return {"mensaje": "Chrome listo y OSIPTEL cargado"}
        raise HTTPException(status_code=500, detail="Chrome inicio pero OSIPTEL no cargo")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/consultar", response_model=ConsultaResponse)
def consultar(body: ConsultaRequest):
    ruc = body.ruc
    print(f"Consultando RUC: {ruc}")
    resultado = scrape_ruc(ruc)
    counts = resultado.get("counts", {})
    return ConsultaResponse(
        ruc=ruc,
        estado=resultado["estado"],
        mensaje=resultado["mensaje"],
        q_entel=counts.get("ENTEL", 0),
        q_claro=counts.get("CLARO", 0),
        q_movistar=counts.get("MOVISTAR", 0),
        q_bitel=counts.get("BITEL", 0),
        q_wom=counts.get("WOM", 0),
        q_otros=counts.get("OTROS", 0),
        q_total=sum(counts.values()),
    )

@app.post("/reiniciar-chrome")
def reiniciar_chrome():
    try:
        ok = driver_mgr.reiniciar()
        return {"mensaje": "Chrome reiniciado", "osiptel_cargado": ok}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
@app.get("/diagnostico")
def diagnostico():
    """Verifica si Chrome puede acceder a internet y a OSIPTEL."""
    drv = driver_mgr.get()
    resultados = {}
    
    # Test 1: Google
    try:
        drv.get("https://www.google.com")
        WebDriverWait(drv, 15).until(EC.presence_of_element_located((By.NAME, "q")))
        resultados["google"] = "OK"
    except Exception as e:
        resultados["google"] = f"FALLO: {str(e)[:100]}"
    
    # Test 2: OSIPTEL
    try:
        drv.get("https://checatuslineas.osiptel.gob.pe/")
        WebDriverWait(drv, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        resultados["osiptel_body"] = "OK"
        resultados["osiptel_title"] = drv.title
        resultados["osiptel_url"] = drv.current_url
        # Ver si el formulario existe
        try:
            drv.find_element(By.ID, "IdTipoDoc")
            resultados["formulario"] = "OK"
        except:
            resultados["formulario"] = "NO ENCONTRADO"
            resultados["page_source_inicio"] = drv.page_source[:500]
    except Exception as e:
        resultados["osiptel_body"] = f"FALLO: {str(e)[:100]}"
    
    return resultados
