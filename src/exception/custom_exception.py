from xml.dom.minidom import Entity


class EntityNotFound(Exception):
    def __init__(self, message: str = "Entity not found"):
        self.message = message
        super().__init__(self.message)

class EntityAlreadyExists(Exception):
    def __init__(self, message: str = "Entity already exists"):
        self.message = message
        super().__init__(self.message)

class InvalidFileType(Exception):
    def __init__(self, message: str = "Invalid file type"):
        self.message = message
        super().__init__(self.message)

class InvalidFileSize(Exception):
    def __init__(self, message: str = "Invalid file size"):
        self.message = message
        super().__init__(self.message)
