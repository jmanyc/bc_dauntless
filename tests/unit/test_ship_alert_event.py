"""ET_SET_ALERT_LEVEL sent to a ship sets its alert level (native ShipClass
handling). QuickBattle.StartSimulation sends RED_ALERT to the player this way,
and XOMenuHandlers.SetAlertLevel forwards the XO's alert buttons the same way;
without it a Quick Battle started at green alert with shields down."""
import App
from engine.appc.ships import ShipClass, ShipClass_Create


def _send(ship, level):
    ev = App.TGIntEvent_Create()
    ev.SetDestination(ship)
    ev.SetEventType(App.ET_SET_ALERT_LEVEL)
    ev.SetInt(level)
    ship.ProcessEvent(ev)


def test_alert_event_sets_red_alert():
    ship = ShipClass_Create("Galaxy")
    ship.SetAlertLevel(ShipClass.GREEN_ALERT)
    _send(ship, ShipClass.RED_ALERT)
    assert ship.GetAlertLevel() == ShipClass.RED_ALERT


def test_handler_that_stops_the_chain_wins():
    ship = ShipClass_Create("Galaxy")
    ship.SetAlertLevel(ShipClass.GREEN_ALERT)
    ship.AddPythonFuncHandlerForInstance(App.ET_SET_ALERT_LEVEL, __name__ + "._swallow")
    _send(ship, ShipClass.RED_ALERT)
    assert ship.GetAlertLevel() == ShipClass.GREEN_ALERT


def _swallow(pObject, pEvent):
    pass                                    # no CallNextHandler: chain stops
