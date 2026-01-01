from abc import ABC, abstractmethod

class BaseIngestor(ABC):

    @abstractmethod
    async def run(self):
        """
        Método abstracto que debe ser implementado por todas las clases hijas.
        Define la lógica principal del ingestor.
        """
        pass


    @abstractmethod
    def close(self):
        """
        Método abstracto para cerrar y limpiar recursos.
        Debe ser implementado por todas las clases hijas.
        """
        pass


