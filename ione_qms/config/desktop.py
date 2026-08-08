from frappe import _


def get_data():
	return [
		{
			"module_name": "IONE QMS",
			"type": "module",
			"label": _("医疗质量管理"),
			"color": "#165D80",
			"icon": "octicon octicon-shield-check",
			"description": _("医院医疗质量管理与持续改进"),
		}
	]
