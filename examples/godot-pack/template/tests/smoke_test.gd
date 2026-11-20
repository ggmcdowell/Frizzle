extends SceneTree
# The headless smoke test: tools/gate.sh runs it with godot --headless --script res://tests/smoke_test.gd.
# It must print one SMOKE PASS line and exit 0, else the gate is red. Add your checks before the verdict.


func _init() -> void:
	var failures: Array[String] = []
	if 2 + 2 != 4:
		failures.append("arithmetic")
	if failures.is_empty():
		print("SMOKE PASS")
		quit(0)
	else:
		print("SMOKE FAIL: ", ", ".join(failures))
		quit(1)
