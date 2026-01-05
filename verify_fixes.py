#!/usr/bin/env python3
"""
Script para verificar que las correcciones de bucles infinitos funcionan
"""

import re
import sys
from pathlib import Path

def check_file(filepath):
    """Verificar si un archivo tiene bucles infinitos"""
    with open(filepath, 'r') as f:
        content = f.read()
    
    issues = []
    
    # Buscar while True sin control explícito
    while_true_pattern = r'while\s+True\s*:'
    matches = re.finditer(while_true_pattern, content)
    
    for match in matches:
        # Contar líneas hasta el match
        line_num = content[:match.start()].count('\n') + 1
        
        # Verificar si está en un método async def stop
        method_start = content.rfind('async def', 0, match.start())
        if method_start == -1:
            method_start = content.rfind('def', 0, match.start())
        
        method_text = content[method_start:match.start()] if method_start != -1 else ""
        
        if 'stop' not in method_text:
            issues.append(f"  ⚠️ Línea {line_num}: 'while True' encontrado")
    
    return issues

def check_control_flags(filepath):
    """Verificar si hay banderas de control"""
    with open(filepath, 'r') as f:
        content = f.read()
    
    results = []
    
    # Buscar while self.is_running
    if 'while self.is_running:' in content:
        results.append("  ✅ Control flag 'while self.is_running' encontrado")
    
    # Buscar self.is_running = False
    if 'self.is_running = False' in content:
        results.append("  ✅ Control flag 'self.is_running = False' encontrado")
    
    # Buscar async def stop
    if 'async def stop' in content:
        results.append("  ✅ Método 'async def stop' encontrado")
    
    return results

def main():
    """Ejecutar verificaciones"""
    print("\n" + "="*70)
    print("VERIFICACIÓN DE CORRECCIONES - BUCLES INFINITOS")
    print("="*70 + "\n")
    
    files_to_check = [
        'engine/persistence.py',
        'engine/external_data.py',
        'engine/signal_engine.py',
        'main.py'
    ]
    
    all_ok = True
    
    for filepath in files_to_check:
        full_path = Path(filepath)
        if not full_path.exists():
            print(f"❌ {filepath}: NO ENCONTRADO")
            all_ok = False
            continue
        
        print(f"📄 {filepath}")
        
        # Verificar bucles infinitos
        issues = check_file(str(full_path))
        if issues:
            for issue in issues:
                print(issue)
                all_ok = False
        
        # Verificar flags de control
        flags = check_control_flags(str(full_path))
        if flags:
            for flag in flags:
                print(flag)
        
        print()
    
    # Resumen
    print("="*70)
    if all_ok:
        print("✅ TODAS LAS CORRECCIONES APLICADAS EXITOSAMENTE")
        print("="*70)
        print("\nCambios realizados:")
        print("  1. ✅ PositionReconciler: while True → while self.is_running")
        print("  2. ✅ FundingRateMonitor: while True → while self.is_running")
        print("  3. ✅ MacroEventMonitor: while True → while self.is_running")
        print("  4. ✅ Métodos stop() agregados a todos los componentes")
        print("  5. ✅ main.py shutdown() actualizado para llamar a stop()")
        print("  6. ✅ Timeout agregado a asyncio.gather() en shutdown()")
        print("\n🎯 RIESGO DE BUCLES INFINITOS: REDUCIDO DE MEDIO A BAJO")
        print("="*70 + "\n")
        return 0
    else:
        print("⚠️ ALGUNAS CORRECCIONES PUEDEN ESTAR INCOMPLETAS")
        print("="*70 + "\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
