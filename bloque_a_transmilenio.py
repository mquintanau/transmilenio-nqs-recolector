"""Reconstrucción reproducible del Bloque A del avance del 24/11/2026.
Datos académicos transcritos del PDF; no son mediciones de demanda real.
Requiere scipy >= 1.9 y numpy. Ejecutar: python bloque_a_transmilenio.py
"""
import numpy as np
from scipy.optimize import milp, Bounds, LinearConstraint

# 1. Insumos: orden de paradas de ida (nombres abreviados uniformemente).
SERVICIOS = ('4', 'B12', 'E42', 'M47')
RUTAS = {
    '4': ['Portal Sur', 'Perdomo', 'Madelena', 'Sevillana', 'Venecia', 'Alquería', 'General Santander', 'NQS Calle 38A Sur–Centro Mayor', 'NQS Calle 30 Sur', 'Santa Isabel', 'Comuneros', 'Ricaurte', 'Paloquemao', 'CAD', 'Av. El Dorado', 'Universidad Nacional', 'Campín–UAN', 'TIGO', '7 de Agosto', 'Avenida Chile', 'NQS Calle 75', 'Escuela Militar', 'Polo–Fincomercio', 'Héroes–Colmena Seguros'],
    'B12': ['Portal Sur', 'Perdomo', 'General Santander', 'Comuneros', 'Ricaurte', 'Av. El Dorado', 'Universidad Nacional', '7 de Agosto', 'NQS Calle 75', 'La Castellana', 'Calle 100', 'Calle 127', 'Prado', 'Calle 142', 'Toberín', 'Portal Norte'],
    'E42': ['San Mateo', 'Terreros–Hospital Cardiovascular', 'León XIII', 'La Despensa', 'Bosa', 'Comuneros', 'Paloquemao', 'Universidad Nacional', 'Campín–UAN', '7 de Agosto'],
    'M47': ['Portal Sur', 'Perdomo', 'Madelena', 'Sevillana', 'NQS Calle 38A Sur–Centro Mayor', 'Comuneros', 'Guatoque–Veraguas', 'Tygua–San José', 'San Victorino', 'Las Nieves', 'San Diego', 'Museo Nacional'],
}
# Sena figura en infraestructura, no como parada habilitada.
INFRAESTRUCTURA_SENA = ('NQS Calle 30 Sur', 'Sena', 'Santa Isabel')
RETORNOS = {'4':'4', 'B12':'G12', 'E42':'S42', 'M47':'G47'}
# Minutos por ciclo: NQS, exterior, regulación.
TIEMPOS = {'4': (86,8,10), 'B12': (72,55,9), 'E42': (69,17,10), 'M47': (50,24,10)}
DEMANDA = [
    ('Portal Sur','Universidad Nacional',110),
    ('General Santander','7 de Agosto',100),
    ('Madelena','CAD',85),
    ('Paloquemao','Campín–UAN',70),
    ('San Mateo','Universidad Nacional',140),
    ('Bosa','Comuneros',85),
    ('Portal Sur','Tygua–San José',75),
    ('Sevillana','Guatoque–Veraguas',65),
    ('Ricaurte','La Castellana',80),
]
CUPO = 60
FLOTA = 27
TOTAL = sum(d for _,_,d in DEMANDA)
assert TOTAL == 810

# 2. Compatibilidad origen-destino y arcos ocupados por cada viaje.
ARCOS = {s:list(zip(p[:-1],p[1:])) for s,p in RUTAS.items()}
OCUPACION = {}
for k,(o,d,_) in enumerate(DEMANDA):
    for s,p in RUTAS.items():
        if o in p and d in p and p.index(o) < p.index(d):
            OCUPACION[k,s] = set(range(p.index(o),p.index(d)))
assert all(any((k,s) in OCUPACION for s in SERVICIOS) for k in range(len(DEMANDA)))

# 3. Variables enteras no negativas, con y solo para viajes directos posibles.
VARIABLES = ([('x',s) for s in SERVICIOS] + [('n',s) for s in SERVICIOS]
             + [('y',k,s) for k,s in OCUPACION] + [('u',k) for k in range(len(DEMANDA))])
IDX = {v:i for i,v in enumerate(VARIABLES)}

def vector(coefs):
    a = np.zeros(len(VARIABLES))
    for v,c in coefs.items():
        a[IDX[v]] = c
    return a


def resolver(lluvia=0., fijar=None):
    """Minimiza lexicográficamente (sin cupo, buses, salidas).
    fijar: diccionario opcional {('x','E42'): 3}, etc.
    """
    if lluvia < 0:
        raise ValueError('La lluvia debe ser no negativa.')
    ciclos = {s:round((1+lluvia)*a+b+c, 8) for s,(a,b,c) in TIEMPOS.items()}
    filas, inferiores, superiores = [], [], []
    def agregar(coefs, lo=-np.inf, hi=np.inf):
        filas.append(vector(coefs)); inferiores.append(lo); superiores.append(hi)
    # Conservación de demanda por par.
    for k,(_,_,d) in enumerate(DEMANDA):
        agregar({('u',k):1, **{('y',k,s):1 for s in SERVICIOS if (k,s) in OCUPACION}},d,d)
    # Capacidad de cada tramo; el cupo se reutiliza tras un descenso.
    for s in SERVICIOS:
        for a in range(len(ARCOS[s])):
            agregar({('x',s):-CUPO, **{('y',k,t):1 for (k,t),tramos in OCUPACION.items() if t==s and a in tramos}},hi=0)
    # Ciclos de ida/vuelta y flota de refuerzo compartida.
    for s in SERVICIOS:
        agregar({('x',s):ciclos[s], ('n',s):-60},hi=0)
    agregar({('n',s):1 for s in SERVICIOS},hi=FLOTA)
    for v,valor in (fijar or {}).items():
        agregar({v:1},valor,valor)
    objetivos = [{('u',k):1 for k in range(len(DEMANDA))},
                 {('n',s):1 for s in SERVICIOS}, {('x',s):1 for s in SERVICIOS}]
    optimos = []
    for objetivo in objetivos:
        c = vector(objetivo)
        res = milp(c, integrality=np.ones(len(VARIABLES)),
                   bounds=Bounds(np.zeros(len(VARIABLES)), np.full(len(VARIABLES),np.inf)),
                   constraints=LinearConstraint(np.array(filas), inferiores, superiores),
                   options={'mip_rel_gap':0.0})
        if not res.success:
            raise RuntimeError(res.message)
        z = np.rint(res.x).astype(int)
        opt = int(c @ z)
        optimos.append(opt)
        agregar(objetivo,opt,opt) # Fijar el óptimo antes de la siguiente etapa.
    assert np.all(np.array(filas) @ z >= np.array(inferiores)-1e-6)
    assert np.all(np.array(filas) @ z <= np.array(superiores)+1e-6)
    valores = dict(zip(VARIABLES,map(int,z)))
    return {'lluvia':lluvia, 'ciclos':ciclos,
            'x':tuple(valores['x',s] for s in SERVICIOS),
            'n':tuple(valores['n',s] for s in SERVICIOS),
            'atendidos':TOTAL-optimos[0], 'sin_cupo':optimos[0],
            'objetivos':tuple(optimos), 'variables':valores}


def mostrar(r):
    print(f"Lluvia {r['lluvia']:.0%}: x={r['x']}, n={r['n']}, buses={sum(r['n'])}, atendidos={r['atendidos']}, sin cupo={r['sin_cupo']}")
    for k,(o,d,dem) in enumerate(DEMANDA):
        u = r['variables']['u',k]
        asignacion = ', '.join(f"{s}: {r['variables']['y',k,s]}" for s in SERVICIOS if (k,s) in OCUPACION)
        print(f'  {o} → {d} ({dem}): {asignacion}; sin cupo: {u}')


def main():
    print('Orden de servicios:', SERVICIOS)
    for lluvia in (0,.2,.4):
        mostrar(resolver(lluvia))
    print('\nEfecto de fijar x_E42=3 con lluvia del 20%:')
    mostrar(resolver(.2,{('x','E42'):3}))

if __name__ == '__main__':
    main()
